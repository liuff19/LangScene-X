#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import math

import torch
from diff_LangSurf_rasterization import \
    GaussianRasterizationSettings as PlaneGaussianRasterizationSettings
from diff_LangSurf_rasterization import \
    GaussianRasterizer as PlaneGaussianRasterizer

from field_construction.scene.app_model import AppModel
from field_construction.scene.gaussian_model import GaussianModel
from field_construction.utils.graphics_utils import normal_from_depth_image
from field_construction.utils.pose_utils import (get_camera_from_tensor,
                                                 quadmultiply)
from field_construction.utils.sh_utils import eval_sh


def render_normal(viewpoint_cam, depth, offset=None, normal=None, scale=1):
    # depth: (H, W), bg_color: (3), alpha: (H, W)
    # normal_ref: (3, H, W)
    intrinsic_matrix, extrinsic_matrix = viewpoint_cam.get_calib_matrix_nerf(scale=scale)
    st = max(int(scale/2)-1,0)
    if offset is not None:
        offset = offset[st::scale,st::scale]
    normal_ref = normal_from_depth_image(depth[st::scale,st::scale], 
                                            intrinsic_matrix.to(depth.device), 
                                            extrinsic_matrix.to(depth.device), offset)

    normal_ref = normal_ref.permute(2,0,1)
    return normal_ref

def render(
    viewpoint_camera,
    pc : GaussianModel,
    pipe, 
    bg_color : torch.Tensor, 
    scaling_modifier=1.0, 
    override_color=None, 
    app_model: AppModel=None,
    return_plane=True,
    return_depth_normal=True, 
    include_feature=True, 
    camera_pose=None
):
    """
    Render the scene. 
    
    Background tensor (bg_color) must be on GPU!
    """
    # Create zero tensor. We will use it to make pytorch return gradients of the 2D (screen-space) means
    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0
    screenspace_points_abs = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0
    try:
        screenspace_points.retain_grad()
        screenspace_points_abs.retain_grad()
    except:
        pass

    # Set up rasterization configuration
    tanfovx = math.tan(viewpoint_camera.FoVx * 0.5)
    tanfovy = math.tan(viewpoint_camera.FoVy * 0.5)

    w2c = torch.eye(4).cuda()
    projmatrix = (
        w2c.unsqueeze(0).bmm(viewpoint_camera.projection_matrix.unsqueeze(0))
    ).squeeze(0)
    camera_pos = w2c.inverse()[3, :3]
    
    if camera_pose is not None:
        rel_w2c = get_camera_from_tensor(camera_pose)
        gaussians_xyz = pc._xyz.clone()
        gaussians_rot = pc._rotation.clone()
        xyz_ones = torch.ones(gaussians_xyz.shape[0], 1).cuda().float()
        xyz_homo = torch.cat((gaussians_xyz, xyz_ones), dim=1)
        gaussians_xyz_trans = (rel_w2c @ xyz_homo.T).T[:, :3]
        gaussians_rot_trans = quadmultiply(camera_pose[:4], gaussians_rot)
        means3D = gaussians_xyz_trans 
    
    else:
        means3D = pc.get_xyz

    means2D = screenspace_points
    means2D_abs = screenspace_points_abs
    opacity = pc.get_opacity
    
    # If precomputed 3d covariance is provided, use it. If not, then it will be computed from
    # scaling / rotation by the rasterizer.
    scales = None
    rotations = None
    cov3D_precomp = None
    if pipe.compute_cov3D_python:
        cov3D_precomp = pc.get_covariance(scaling_modifier)
    else:
        scales = pc.get_scaling
        rotations = gaussians_rot_trans if camera_pose is not None else pc.get_rotation
        # rotations = pc.get_rotation
    
    # If precomputed colors are provided, use them. Otherwise, if it is desired to precompute colors
    # from SHs in Python, do it. If not, then SH -> RGB conversion will be done by rasterizer.
    shs = None
    colors_precomp = None

    if override_color is None:
        if pipe.convert_SHs_python:
            shs_view = pc.get_features.transpose(1, 2).view(-1, 3, (pc.max_sh_degree+1)**2)
            dir_pp = (pc.get_xyz - viewpoint_camera.camera_center.repeat(pc.get_features.shape[0], 1))
            dir_pp_normalized = dir_pp/dir_pp.norm(dim=1, keepdim=True)
            sh2rgb = eval_sh(pc.active_sh_degree, shs_view, dir_pp_normalized)
            colors_precomp = torch.clamp_min(sh2rgb + 0.5, 0.0)
        else:
            shs = pc.get_features
    else:
        colors_precomp = override_color

    if include_feature:
        language_feature_precomp = pc.get_language_feature
        instance_feature_precomp = pc.get_instance_feature
        # language_feature_precomp = language_feature_precomp / (language_feature_precomp.norm(dim=-1, keepdim=True) + 1e-9)
        # instance_feature_precomp = instance_feature_precomp / (instance_feature_precomp.norm(dim=-1, keepdim=True) + 1e-9)
        # language_feature_precomp = torch.sigmoid(language_feature_precomp)
    else:
        language_feature_precomp = torch.zeros((1,), dtype=opacity.dtype, device=opacity.device)
        instance_feature_precomp = torch.zeros((1,), dtype=opacity.dtype, device=opacity.device)

    return_dict = None
    raster_settings = PlaneGaussianRasterizationSettings(
            image_height=int(viewpoint_camera.image_height),
            image_width=int(viewpoint_camera.image_width),
            tanfovx=tanfovx,
            tanfovy=tanfovy,
            bg=bg_color,
            scale_modifier=scaling_modifier,
            # viewmatrix=viewpoint_camera.world_view_transform,
            # projmatrix=viewpoint_camera.full_proj_transform,
            viewmatrix=w2c if camera_pose is not None else viewpoint_camera.world_view_transform,
            projmatrix=projmatrix if camera_pose is not None else viewpoint_camera.full_proj_transform,
            sh_degree=pc.active_sh_degree,
            # campos=viewpoint_camera.camera_center,
            campos=camera_pos if camera_pose is not None else viewpoint_camera.camera_center,
            prefiltered=False,
            render_geo=return_plane,
            debug=pipe.debug,
            include_feature=include_feature,
        )

    rasterizer = PlaneGaussianRasterizer(raster_settings=raster_settings)


    if not return_plane:
        rendered_image, language_feature, instance_feature, radii, out_observe, _, _ = rasterizer(
            means3D = means3D,
            means2D = means2D,
            means2D_abs = means2D_abs,
            shs = shs,
            colors_precomp = colors_precomp,
            language_feature_precomp = language_feature_precomp,
            language_feature_instance_precomp = instance_feature_precomp,
            opacities = opacity,
            scales = scales,
            rotations = rotations,
            cov3D_precomp = cov3D_precomp)
        
        return_dict =  {"render": rendered_image,
                        "viewspace_points": screenspace_points,
                        "viewspace_points_abs": screenspace_points_abs,
                        "visibility_filter" : radii > 0,
                        "radii": radii,
                        "out_observe": out_observe,
                        "language_feature": language_feature,
                        "instance_feature": instance_feature,
                        }
        if app_model is not None and pc.use_app:
            appear_ab = app_model.appear_ab[torch.tensor(viewpoint_camera.uid).cuda()]
            app_image = torch.exp(appear_ab[0]) * rendered_image + appear_ab[1]
            return_dict.update({"app_image": app_image})
        return return_dict

    global_normal = pc.get_normal(viewpoint_camera)
    local_normal = global_normal @ viewpoint_camera.world_view_transform[:3,:3]
    pts_in_cam = means3D @ viewpoint_camera.world_view_transform[:3,:3] + viewpoint_camera.world_view_transform[3,:3]
    depth_z = pts_in_cam[:, 2]
    local_distance = (local_normal * pts_in_cam).sum(-1).abs()
    input_all_map = torch.zeros((means3D.shape[0], 5)).cuda().float()
    input_all_map[:, :3] = local_normal
    input_all_map[:, 3] = 1.0
    input_all_map[:, 4] = local_distance

    rendered_image, language_feature, instance_feature, radii, out_observe, out_all_map, plane_depth = rasterizer(
        means3D = means3D,
        means2D = means2D,
        means2D_abs = means2D_abs,
        shs = shs,
        colors_precomp = colors_precomp,
        language_feature_precomp = language_feature_precomp,
        language_feature_instance_precomp = instance_feature_precomp,
        opacities = opacity,
        scales = scales,
        rotations = rotations,
        all_map = input_all_map,
        cov3D_precomp = cov3D_precomp)

    rendered_normal = out_all_map[0:3]
    rendered_alpha = out_all_map[3:4, ]
    rendered_distance = out_all_map[4:5, ]
    
    return_dict =  {"render": rendered_image,
                    "viewspace_points": screenspace_points,
                    "viewspace_points_abs": screenspace_points_abs,
                    "visibility_filter" : radii > 0,
                    "radii": radii,
                    "out_observe": out_observe,
                    "rendered_normal": rendered_normal,
                    "plane_depth": plane_depth,
                    "rendered_distance": rendered_distance,
                    "language_feature": language_feature,
                    "instance_feature": instance_feature,
                    }
    if app_model is not None:
        appear_ab = app_model.appear_ab[torch.tensor(viewpoint_camera.uid).cuda()]
        app_image = torch.exp(appear_ab[0]) * rendered_image + appear_ab[1]
        return_dict.update({"app_image": app_image})   

    if return_depth_normal:
        depth_normal = render_normal(viewpoint_camera, plane_depth.squeeze()) * (rendered_alpha).detach()
        return_dict.update({"depth_normal": depth_normal})
    
    # Those Gaussians that were frustum culled or had a radius of 0 were not visible.
    # They will be excluded from value updates used in the splitting criteria.
    return return_dict