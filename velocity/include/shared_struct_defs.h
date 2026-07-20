#ifndef __SHARED_STRUCT_DEFS_H__
#define __SHARED_STRUCT_DEFS_H__

#include <dace/dace.h>


/*
struct global_data_type
struct t_tangent_vectors
struct t_grid_edges
struct t_nh_metrics
struct t_prepare_adv
struct t_nh_ref
struct t_nh_prog
struct t_nh_diag
struct t_nh_state
struct t_grid_domain_decomp_info
struct t_grid_cells
struct t_int_state
struct t_grid_vertices
struct t_patch
*/

struct global_data_type {
  // double fields
  double divdamp_fac = {};    // corrector_pre
  double divdamp_fac_o2 = {}; // corrector_pre
  double iau_wgt_dyn =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre

  // int fields
  int divdamp_order = {};   // corrector_pre
  int divdamp_type = {};    // predictor_post, corrector_pre
  int grf_intmethod_e = {}; // predictor_pre, corrector_post, predictor_post
  int i_am_accel_node = {}; // predictor_pre, corrector_pre
  int iadv_rhotheta = {};   // predictor_pre
  int igradp_method = {};   // predictor_pre
  int is_iau_active =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre
  int itime_scheme = {}; // predictor_pre, corrector_post, corrector_pre
  int l_limited_area =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre
  int ldeepatmo = {};    // predictor_pre, corrector_pre
  int lextra_diffu = {}; // predictor_pre, corrector_pre
  int lvert_nest = {};   // predictor_pre, corrector_pre
  int nproma =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre
  int rayleigh_type =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre
  int timer_intp = {};              // predictor_pre, corrector_pre
  int timer_solve_nh_cellcomp = {}; // predictor_pre, corrector_pre
  int timer_solve_nh_edgecomp = {}; // corrector_post, predictor_post
  int timer_solve_nh_veltend = {};  // predictor_pre, corrector_pre
  int timer_solve_nh_vimpl = {};    // corrector_post, predictor_post
  int timer_solve_nh_vnupd = {};    // predictor_pre, corrector_pre
  int timers_level =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre

  // pointer fields
  int *kstart_dd3d =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre
  int *kstart_moist = {};      // corrector_post, predictor_post
  int *ndyn_substeps_var = {}; // corrector_post, predictor_post
  int *nflat_gradp = {};       // predictor_pre
  int *nflatlev =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre
  int *nrdmax =
      {}; // predictor_pre, corrector_post, predictor_post, corrector_pre
};

struct t_tangent_vectors {
  double v1 = {};
  double v2 = {};
};

struct t_grid_edges {
  union {
    int __f2dace_SA_area_edge_d_0_s_188;
    int __f2dace_SA_area_edge_d_0_s;
  };
  union {
    int __f2dace_SA_area_edge_d_1_s_189;
    int __f2dace_SA_area_edge_d_1_s;
  };
  union {
    int __f2dace_SA_cell_blk_d_0_s_167;
    int __f2dace_SA_cell_blk_d_0_s_203;
    int __f2dace_SA_cell_blk_d_0_s;
  };
  union {
    int __f2dace_SA_cell_blk_d_1_s_168;
    int __f2dace_SA_cell_blk_d_1_s_204;
    int __f2dace_SA_cell_blk_d_1_s;
  };
  union {
    int __f2dace_SA_cell_blk_d_2_s_169;
    int __f2dace_SA_cell_blk_d_2_s_205;
    int __f2dace_SA_cell_blk_d_2_s;
  };
  union {
    int __f2dace_SA_cell_idx_d_0_s_164;
    int __f2dace_SA_cell_idx_d_0_s_200;
    int __f2dace_SA_cell_idx_d_0_s;
  };
  union {
    int __f2dace_SA_cell_idx_d_1_s_165;
    int __f2dace_SA_cell_idx_d_1_s_201;
    int __f2dace_SA_cell_idx_d_1_s;
  };
  union {
    int __f2dace_SA_cell_idx_d_2_s_166;
    int __f2dace_SA_cell_idx_d_2_s_202;
    int __f2dace_SA_cell_idx_d_2_s;
  };
  int __f2dace_SA_dual_normal_cell_d_0_s = {};
  int __f2dace_SA_dual_normal_cell_d_1_s = {};
  int __f2dace_SA_dual_normal_cell_d_2_s = {};
  union {
    int __f2dace_SA_end_block_d_0_s_199;
    int __f2dace_SA_end_block_d_0_s;
  };
  union {
    int __f2dace_SA_end_index_d_0_s_197;
    int __f2dace_SA_end_index_d_0_s;
  };
  union {
    int __f2dace_SA_f_e_d_0_s_190;
    int __f2dace_SA_f_e_d_0_s;
  };
  union {
    int __f2dace_SA_f_e_d_1_s_191;
    int __f2dace_SA_f_e_d_1_s;
  };
  union {
    int __f2dace_SA_fn_e_d_0_s_192;
    int __f2dace_SA_fn_e_d_0_s;
  };
  union {
    int __f2dace_SA_fn_e_d_1_s_193;
    int __f2dace_SA_fn_e_d_1_s;
  };
  union {
    int __f2dace_SA_ft_e_d_0_s_194;
    int __f2dace_SA_ft_e_d_0_s;
  };
  union {
    int __f2dace_SA_ft_e_d_1_s_195;
    int __f2dace_SA_ft_e_d_1_s;
  };
  union {
    int __f2dace_SA_inv_dual_edge_length_d_0_s_186;
    int __f2dace_SA_inv_dual_edge_length_d_0_s;
  };
  union {
    int __f2dace_SA_inv_dual_edge_length_d_1_s_187;
    int __f2dace_SA_inv_dual_edge_length_d_1_s;
  };
  union {
    int __f2dace_SA_inv_primal_edge_length_d_0_s_184;
    int __f2dace_SA_inv_primal_edge_length_d_0_s;
  };
  union {
    int __f2dace_SA_inv_primal_edge_length_d_1_s_185;
    int __f2dace_SA_inv_primal_edge_length_d_1_s;
  };
  int __f2dace_SA_primal_normal_cell_d_0_s = {};
  int __f2dace_SA_primal_normal_cell_d_1_s = {};
  int __f2dace_SA_primal_normal_cell_d_2_s = {};
  union {
    int __f2dace_SA_quad_blk_d_0_s_181;
    int __f2dace_SA_quad_blk_d_0_s_183;
    int __f2dace_SA_quad_blk_d_0_s;
  };
  union {
    int __f2dace_SA_quad_blk_d_1_s_182;
    int __f2dace_SA_quad_blk_d_1_s;
  };
  union {
    int __f2dace_SA_quad_blk_d_2_s_183;
    int __f2dace_SA_quad_blk_d_2_s;
  };
  union {
    int __f2dace_SA_quad_idx_d_0_s_178;
    int __f2dace_SA_quad_idx_d_0_s;
  };
  union {
    int __f2dace_SA_quad_idx_d_1_s_179;
    int __f2dace_SA_quad_idx_d_1_s;
  };
  union {
    int __f2dace_SA_quad_idx_d_2_s_180;
    int __f2dace_SA_quad_idx_d_2_s;
  };
  int __f2dace_SA_refin_ctrl_d_0_s = {};
  int __f2dace_SA_refin_ctrl_d_1_s = {};
  union {
    int __f2dace_SA_start_block_d_0_s_198;
    int __f2dace_SA_start_block_d_0_s;
  };
  union {
    int __f2dace_SA_start_index_d_0_s_196;
    int __f2dace_SA_start_index_d_0_s;
  };
  union {
    int __f2dace_SA_tangent_orientation_d_0_s_176;
    int __f2dace_SA_tangent_orientation_d_0_s;
  };
  union {
    int __f2dace_SA_tangent_orientation_d_1_s_177;
    int __f2dace_SA_tangent_orientation_d_1_s;
  };
  union {
    int __f2dace_SA_vertex_blk_d_0_s_173;
    int __f2dace_SA_vertex_blk_d_0_s;
  };
  union {
    int __f2dace_SA_vertex_blk_d_1_s_174;
    int __f2dace_SA_vertex_blk_d_1_s;
  };
  union {
    int __f2dace_SA_vertex_blk_d_2_s_175;
    int __f2dace_SA_vertex_blk_d_2_s;
  };
  union {
    int __f2dace_SA_vertex_idx_d_0_s_170;
    int __f2dace_SA_vertex_idx_d_0_s;
  };
  union {
    int __f2dace_SA_vertex_idx_d_1_s_171;
    int __f2dace_SA_vertex_idx_d_1_s;
  };
  union {
    int __f2dace_SA_vertex_idx_d_2_s_172;
    int __f2dace_SA_vertex_idx_d_2_s;
  };
  union {
    int __f2dace_SOA_area_edge_d_0_s_188;
    int __f2dace_SOA_area_edge_d_0_s;
  };
  union {
    int __f2dace_SOA_area_edge_d_1_s_189;
    int __f2dace_SOA_area_edge_d_1_s;
  };
  union {
    int __f2dace_SOA_cell_blk_d_0_s_167;
    int __f2dace_SOA_cell_blk_d_0_s_203;
    int __f2dace_SOA_cell_blk_d_0_s;
  };
  union {
    int __f2dace_SOA_cell_blk_d_1_s_168;
    int __f2dace_SOA_cell_blk_d_1_s_204;
    int __f2dace_SOA_cell_blk_d_1_s;
  };
  union {
    int __f2dace_SOA_cell_blk_d_2_s_169;
    int __f2dace_SOA_cell_blk_d_2_s_205;
    int __f2dace_SOA_cell_blk_d_2_s;
  };
  union {
    int __f2dace_SOA_cell_idx_d_0_s_164;
    int __f2dace_SOA_cell_idx_d_0_s_200;
    int __f2dace_SOA_cell_idx_d_0_s;
  };
  union {
    int __f2dace_SOA_cell_idx_d_1_s_165;
    int __f2dace_SOA_cell_idx_d_1_s_201;
    int __f2dace_SOA_cell_idx_d_1_s;
  };
  union {
    int __f2dace_SOA_cell_idx_d_2_s_166;
    int __f2dace_SOA_cell_idx_d_2_s_202;
    int __f2dace_SOA_cell_idx_d_2_s;
  };
  int __f2dace_SOA_dual_normal_cell_d_0_s = {};
  int __f2dace_SOA_dual_normal_cell_d_1_s = {};
  int __f2dace_SOA_dual_normal_cell_d_2_s = {};
  union {
    int __f2dace_SOA_end_block_d_0_s_199;
    int __f2dace_SOA_end_block_d_0_s;
  };
  union {
    int __f2dace_SOA_end_index_d_0_s_197;
    int __f2dace_SOA_end_index_d_0_s;
  };
  union {
    int __f2dace_SOA_f_e_d_0_s_190;
    int __f2dace_SOA_f_e_d_0_s;
  };
  union {
    int __f2dace_SOA_f_e_d_1_s_191;
    int __f2dace_SOA_f_e_d_1_s;
  };
  union {
    int __f2dace_SOA_fn_e_d_0_s_192;
    int __f2dace_SOA_fn_e_d_0_s;
  };
  union {
    int __f2dace_SOA_fn_e_d_1_s_193;
    int __f2dace_SOA_fn_e_d_1_s;
  };
  union {
    int __f2dace_SOA_ft_e_d_0_s_194;
    int __f2dace_SOA_ft_e_d_0_s;
  };
  union {
    int __f2dace_SOA_ft_e_d_1_s_195;
    int __f2dace_SOA_ft_e_d_1_s;
  };
  union {
    int __f2dace_SOA_inv_dual_edge_length_d_0_s_186;
    int __f2dace_SOA_inv_dual_edge_length_d_0_s;
  };
  union {
    int __f2dace_SOA_inv_dual_edge_length_d_1_s_187;
    int __f2dace_SOA_inv_dual_edge_length_d_1_s;
  };
  union {
    int __f2dace_SOA_inv_primal_edge_length_d_0_s_184;
    int __f2dace_SOA_inv_primal_edge_length_d_0_s;
  };
  union {
    int __f2dace_SOA_inv_primal_edge_length_d_1_s_185;
    int __f2dace_SOA_inv_primal_edge_length_d_1_s;
  };
  int __f2dace_SOA_primal_normal_cell_d_0_s = {};
  int __f2dace_SOA_primal_normal_cell_d_1_s = {};
  int __f2dace_SOA_primal_normal_cell_d_2_s = {};
  union {
    int __f2dace_SOA_quad_blk_d_0_s_181;
    int __f2dace_SOA_quad_blk_d_0_s_183;
    int __f2dace_SOA_quad_blk_d_0_s;
  };
  union {
    int __f2dace_SOA_quad_blk_d_1_s_182;
    int __f2dace_SOA_quad_blk_d_1_s;
  };
  union {
    int __f2dace_SOA_quad_blk_d_2_s_183;
    int __f2dace_SOA_quad_blk_d_2_s;
  };
  union {
    int __f2dace_SOA_quad_idx_d_0_s_178;
    int __f2dace_SOA_quad_idx_d_0_s;
  };
  union {
    int __f2dace_SOA_quad_idx_d_1_s_179;
    int __f2dace_SOA_quad_idx_d_1_s;
  };
  union {
    int __f2dace_SOA_quad_idx_d_2_s_180;
    int __f2dace_SOA_quad_idx_d_2_s;
  };
  int __f2dace_SOA_refin_ctrl_d_0_s = {};
  int __f2dace_SOA_refin_ctrl_d_1_s = {};
  union {
    int __f2dace_SOA_start_block_d_0_s_198;
    int __f2dace_SOA_start_block_d_0_s;
  };
  union {
    int __f2dace_SOA_start_index_d_0_s_196;
    int __f2dace_SOA_start_index_d_0_s;
  };
  union {
    int __f2dace_SOA_tangent_orientation_d_0_s_176;
    int __f2dace_SOA_tangent_orientation_d_0_s;
  };
  union {
    int __f2dace_SOA_tangent_orientation_d_1_s_177;
    int __f2dace_SOA_tangent_orientation_d_1_s;
  };
  union {
    int __f2dace_SOA_vertex_blk_d_0_s_173;
    int __f2dace_SOA_vertex_blk_d_0_s;
  };
  union {
    int __f2dace_SOA_vertex_blk_d_1_s_174;
    int __f2dace_SOA_vertex_blk_d_1_s;
  };
  union {
    int __f2dace_SOA_vertex_blk_d_2_s_175;
    int __f2dace_SOA_vertex_blk_d_2_s;
  };
  union {
    int __f2dace_SOA_vertex_idx_d_0_s_170;
    int __f2dace_SOA_vertex_idx_d_0_s;
  };
  union {
    int __f2dace_SOA_vertex_idx_d_1_s_171;
    int __f2dace_SOA_vertex_idx_d_1_s;
  };
  union {
    int __f2dace_SOA_vertex_idx_d_2_s_172;
    int __f2dace_SOA_vertex_idx_d_2_s;
  };
  double *area_edge = {};
  int *cell_blk = {};
  int *cell_idx = {};
  t_tangent_vectors **dual_normal_cell = {};
  int *end_block = {};
  int *end_index = {};
  double *f_e = {};
  double *fn_e = {};
  double *ft_e = {};
  double *inv_dual_edge_length = {};
  double *inv_primal_edge_length = {};
  t_tangent_vectors **primal_normal_cell = {};
  int *quad_blk = {};
  int *quad_idx = {};
  int *refin_ctrl = {};
  int *start_block = {};
  int *start_index = {};
  double *tangent_orientation = {};
  int *vertex_blk = {};
  int *vertex_idx = {};
};

struct t_nh_metrics {
  int __f2dace_SA_bdy_mflx_e_blk_d_0_s = {};
  int __f2dace_SA_bdy_mflx_e_idx_d_0_s = {};
  union {
    int __f2dace_SA_coeff1_dwdz_d_0_s_332;
    int __f2dace_SA_coeff1_dwdz_d_0_s;
  };
  union {
    int __f2dace_SA_coeff1_dwdz_d_1_s_333;
    int __f2dace_SA_coeff1_dwdz_d_1_s;
  };
  union {
    int __f2dace_SA_coeff1_dwdz_d_2_s_334;
    int __f2dace_SA_coeff1_dwdz_d_2_s;
  };
  union {
    int __f2dace_SA_coeff2_dwdz_d_0_s_335;
    int __f2dace_SA_coeff2_dwdz_d_0_s;
  };
  union {
    int __f2dace_SA_coeff2_dwdz_d_1_s_336;
    int __f2dace_SA_coeff2_dwdz_d_1_s;
  };
  union {
    int __f2dace_SA_coeff2_dwdz_d_2_s_337;
    int __f2dace_SA_coeff2_dwdz_d_2_s;
  };
  union {
    int __f2dace_SA_coeff_gradekin_d_0_s_329;
    int __f2dace_SA_coeff_gradekin_d_0_s;
  };
  union {
    int __f2dace_SA_coeff_gradekin_d_1_s_330;
    int __f2dace_SA_coeff_gradekin_d_1_s;
  };
  union {
    int __f2dace_SA_coeff_gradekin_d_2_s_331;
    int __f2dace_SA_coeff_gradekin_d_2_s;
  };
  int __f2dace_SA_coeff_gradp_d_0_s = {};
  int __f2dace_SA_coeff_gradp_d_1_s = {};
  int __f2dace_SA_coeff_gradp_d_2_s = {};
  int __f2dace_SA_coeff_gradp_d_3_s = {};
  int __f2dace_SA_d2dexdz2_fac1_mc_d_0_s = {};
  int __f2dace_SA_d2dexdz2_fac1_mc_d_1_s = {};
  int __f2dace_SA_d2dexdz2_fac1_mc_d_2_s = {};
  int __f2dace_SA_d2dexdz2_fac2_mc_d_0_s = {};
  int __f2dace_SA_d2dexdz2_fac2_mc_d_1_s = {};
  int __f2dace_SA_d2dexdz2_fac2_mc_d_2_s = {};
  int __f2dace_SA_d_exner_dz_ref_ic_d_0_s = {};
  int __f2dace_SA_d_exner_dz_ref_ic_d_1_s = {};
  int __f2dace_SA_d_exner_dz_ref_ic_d_2_s = {};
  union {
    int __f2dace_SA_ddqz_z_full_e_d_0_s_314;
    int __f2dace_SA_ddqz_z_full_e_d_0_s;
  };
  union {
    int __f2dace_SA_ddqz_z_full_e_d_1_s_315;
    int __f2dace_SA_ddqz_z_full_e_d_1_s;
  };
  union {
    int __f2dace_SA_ddqz_z_full_e_d_2_s_316;
    int __f2dace_SA_ddqz_z_full_e_d_2_s;
  };
  union {
    int __f2dace_SA_ddqz_z_half_d_0_s_317;
    int __f2dace_SA_ddqz_z_half_d_0_s;
  };
  union {
    int __f2dace_SA_ddqz_z_half_d_1_s_318;
    int __f2dace_SA_ddqz_z_half_d_1_s;
  };
  union {
    int __f2dace_SA_ddqz_z_half_d_2_s_319;
    int __f2dace_SA_ddqz_z_half_d_2_s;
  };
  union {
    int __f2dace_SA_ddxn_z_full_d_0_s_308;
    int __f2dace_SA_ddxn_z_full_d_0_s;
  };
  union {
    int __f2dace_SA_ddxn_z_full_d_1_s_309;
    int __f2dace_SA_ddxn_z_full_d_1_s;
  };
  union {
    int __f2dace_SA_ddxn_z_full_d_2_s_310;
    int __f2dace_SA_ddxn_z_full_d_2_s;
  };
  union {
    int __f2dace_SA_ddxt_z_full_d_0_s_311;
    int __f2dace_SA_ddxt_z_full_d_0_s;
  };
  union {
    int __f2dace_SA_ddxt_z_full_d_1_s_312;
    int __f2dace_SA_ddxt_z_full_d_1_s;
  };
  union {
    int __f2dace_SA_ddxt_z_full_d_2_s_313;
    int __f2dace_SA_ddxt_z_full_d_2_s;
  };
  int __f2dace_SA_deepatmo_divh_mc_d_0_s = {};
  int __f2dace_SA_deepatmo_divzl_mc_d_0_s = {};
  int __f2dace_SA_deepatmo_divzu_mc_d_0_s = {};
  union {
    int __f2dace_SA_deepatmo_gradh_ifc_d_0_s_340;
    int __f2dace_SA_deepatmo_gradh_ifc_d_0_s;
  };
  union {
    int __f2dace_SA_deepatmo_gradh_mc_d_0_s_338;
    int __f2dace_SA_deepatmo_gradh_mc_d_0_s;
  };
  union {
    int __f2dace_SA_deepatmo_invr_ifc_d_0_s_341;
    int __f2dace_SA_deepatmo_invr_ifc_d_0_s;
  };
  union {
    int __f2dace_SA_deepatmo_invr_mc_d_0_s_339;
    int __f2dace_SA_deepatmo_invr_mc_d_0_s;
  };
  int __f2dace_SA_exner_exfac_d_0_s = {};
  int __f2dace_SA_exner_exfac_d_1_s = {};
  int __f2dace_SA_exner_exfac_d_2_s = {};
  int __f2dace_SA_exner_ref_mc_d_0_s = {};
  int __f2dace_SA_exner_ref_mc_d_1_s = {};
  int __f2dace_SA_exner_ref_mc_d_2_s = {};
  int __f2dace_SA_hmask_dd3d_d_0_s = {};
  int __f2dace_SA_hmask_dd3d_d_1_s = {};
  int __f2dace_SA_inv_ddqz_z_full_d_0_s = {};
  int __f2dace_SA_inv_ddqz_z_full_d_1_s = {};
  int __f2dace_SA_inv_ddqz_z_full_d_2_s = {};
  int __f2dace_SA_pg_edgeblk_d_0_s = {};
  int __f2dace_SA_pg_edgeidx_d_0_s = {};
  int __f2dace_SA_pg_exdist_d_0_s = {};
  int __f2dace_SA_pg_vertidx_d_0_s = {};
  int __f2dace_SA_rayleigh_vn_d_0_s = {};
  int __f2dace_SA_rayleigh_w_d_0_s = {};
  int __f2dace_SA_rho_ref_mc_d_0_s = {};
  int __f2dace_SA_rho_ref_mc_d_1_s = {};
  int __f2dace_SA_rho_ref_mc_d_2_s = {};
  int __f2dace_SA_rho_ref_me_d_0_s = {};
  int __f2dace_SA_rho_ref_me_d_1_s = {};
  int __f2dace_SA_rho_ref_me_d_2_s = {};
  int __f2dace_SA_scalfac_dd3d_d_0_s = {};
  int __f2dace_SA_theta_ref_ic_d_0_s = {};
  int __f2dace_SA_theta_ref_ic_d_1_s = {};
  int __f2dace_SA_theta_ref_ic_d_2_s = {};
  int __f2dace_SA_theta_ref_mc_d_0_s = {};
  int __f2dace_SA_theta_ref_mc_d_1_s = {};
  int __f2dace_SA_theta_ref_mc_d_2_s = {};
  int __f2dace_SA_theta_ref_me_d_0_s = {};
  int __f2dace_SA_theta_ref_me_d_1_s = {};
  int __f2dace_SA_theta_ref_me_d_2_s = {};
  int __f2dace_SA_vertidx_gradp_d_0_s = {};
  int __f2dace_SA_vertidx_gradp_d_1_s = {};
  int __f2dace_SA_vertidx_gradp_d_2_s = {};
  int __f2dace_SA_vertidx_gradp_d_3_s = {};
  int __f2dace_SA_vwind_expl_wgt_d_0_s = {};
  int __f2dace_SA_vwind_expl_wgt_d_1_s = {};
  int __f2dace_SA_vwind_impl_wgt_d_0_s = {};
  int __f2dace_SA_vwind_impl_wgt_d_1_s = {};
  union {
    int __f2dace_SA_wgtfac_c_d_0_s_320;
    int __f2dace_SA_wgtfac_c_d_0_s;
  };
  union {
    int __f2dace_SA_wgtfac_c_d_1_s_321;
    int __f2dace_SA_wgtfac_c_d_1_s;
  };
  union {
    int __f2dace_SA_wgtfac_c_d_2_s_322;
    int __f2dace_SA_wgtfac_c_d_2_s;
  };
  union {
    int __f2dace_SA_wgtfac_e_d_0_s_323;
    int __f2dace_SA_wgtfac_e_d_0_s;
  };
  union {
    int __f2dace_SA_wgtfac_e_d_1_s_324;
    int __f2dace_SA_wgtfac_e_d_1_s;
  };
  union {
    int __f2dace_SA_wgtfac_e_d_2_s_325;
    int __f2dace_SA_wgtfac_e_d_2_s;
  };
  int __f2dace_SA_wgtfacq1_c_d_0_s = {};
  int __f2dace_SA_wgtfacq1_c_d_1_s = {};
  int __f2dace_SA_wgtfacq1_c_d_2_s = {};
  int __f2dace_SA_wgtfacq_c_d_0_s = {};
  int __f2dace_SA_wgtfacq_c_d_1_s = {};
  int __f2dace_SA_wgtfacq_c_d_2_s = {};
  union {
    int __f2dace_SA_wgtfacq_e_d_0_s_326;
    int __f2dace_SA_wgtfacq_e_d_0_s;
  };
  union {
    int __f2dace_SA_wgtfacq_e_d_1_s_327;
    int __f2dace_SA_wgtfacq_e_d_1_s;
  };
  union {
    int __f2dace_SA_wgtfacq_e_d_2_s_328;
    int __f2dace_SA_wgtfacq_e_d_2_s;
  };
  int __f2dace_SA_zdiff_gradp_d_0_s = {};
  int __f2dace_SA_zdiff_gradp_d_1_s = {};
  int __f2dace_SA_zdiff_gradp_d_2_s = {};
  int __f2dace_SA_zdiff_gradp_d_3_s = {};
  int __f2dace_SOA_bdy_mflx_e_blk_d_0_s = {};
  int __f2dace_SOA_bdy_mflx_e_idx_d_0_s = {};
  union {
    int __f2dace_SOA_coeff1_dwdz_d_0_s_332;
    int __f2dace_SOA_coeff1_dwdz_d_0_s;
  };
  union {
    int __f2dace_SOA_coeff1_dwdz_d_1_s_333;
    int __f2dace_SOA_coeff1_dwdz_d_1_s;
  };
  union {
    int __f2dace_SOA_coeff1_dwdz_d_2_s_334;
    int __f2dace_SOA_coeff1_dwdz_d_2_s;
  };
  union {
    int __f2dace_SOA_coeff2_dwdz_d_0_s_335;
    int __f2dace_SOA_coeff2_dwdz_d_0_s;
  };
  union {
    int __f2dace_SOA_coeff2_dwdz_d_1_s_336;
    int __f2dace_SOA_coeff2_dwdz_d_1_s;
  };
  union {
    int __f2dace_SOA_coeff2_dwdz_d_2_s_337;
    int __f2dace_SOA_coeff2_dwdz_d_2_s;
  };
  union {
    int __f2dace_SOA_coeff_gradekin_d_0_s_329;
    int __f2dace_SOA_coeff_gradekin_d_0_s;
  };
  union {
    int __f2dace_SOA_coeff_gradekin_d_1_s_330;
    int __f2dace_SOA_coeff_gradekin_d_1_s;
  };
  union {
    int __f2dace_SOA_coeff_gradekin_d_2_s_331;
    int __f2dace_SOA_coeff_gradekin_d_2_s;
  };
  int __f2dace_SOA_coeff_gradp_d_0_s = {};
  int __f2dace_SOA_coeff_gradp_d_1_s = {};
  int __f2dace_SOA_coeff_gradp_d_2_s = {};
  int __f2dace_SOA_coeff_gradp_d_3_s = {};
  int __f2dace_SOA_d2dexdz2_fac1_mc_d_0_s = {};
  int __f2dace_SOA_d2dexdz2_fac1_mc_d_1_s = {};
  int __f2dace_SOA_d2dexdz2_fac1_mc_d_2_s = {};
  int __f2dace_SOA_d2dexdz2_fac2_mc_d_0_s = {};
  int __f2dace_SOA_d2dexdz2_fac2_mc_d_1_s = {};
  int __f2dace_SOA_d2dexdz2_fac2_mc_d_2_s = {};
  int __f2dace_SOA_d_exner_dz_ref_ic_d_0_s = {};
  int __f2dace_SOA_d_exner_dz_ref_ic_d_1_s = {};
  int __f2dace_SOA_d_exner_dz_ref_ic_d_2_s = {};
  union {
    int __f2dace_SOA_ddqz_z_full_e_d_0_s_314;
    int __f2dace_SOA_ddqz_z_full_e_d_0_s;
  };
  union {
    int __f2dace_SOA_ddqz_z_full_e_d_1_s_315;
    int __f2dace_SOA_ddqz_z_full_e_d_1_s;
  };
  union {
    int __f2dace_SOA_ddqz_z_full_e_d_2_s_316;
    int __f2dace_SOA_ddqz_z_full_e_d_2_s;
  };
  union {
    int __f2dace_SOA_ddqz_z_half_d_0_s_317;
    int __f2dace_SOA_ddqz_z_half_d_0_s;
  };
  union {
    int __f2dace_SOA_ddqz_z_half_d_1_s_318;
    int __f2dace_SOA_ddqz_z_half_d_1_s;
  };
  union {
    int __f2dace_SOA_ddqz_z_half_d_2_s_319;
    int __f2dace_SOA_ddqz_z_half_d_2_s;
  };
  union {
    int __f2dace_SOA_ddxn_z_full_d_0_s_308;
    int __f2dace_SOA_ddxn_z_full_d_0_s;
  };
  union {
    int __f2dace_SOA_ddxn_z_full_d_1_s_309;
    int __f2dace_SOA_ddxn_z_full_d_1_s;
  };
  union {
    int __f2dace_SOA_ddxn_z_full_d_2_s_310;
    int __f2dace_SOA_ddxn_z_full_d_2_s;
  };
  union {
    int __f2dace_SOA_ddxt_z_full_d_0_s_311;
    int __f2dace_SOA_ddxt_z_full_d_0_s;
  };
  union {
    int __f2dace_SOA_ddxt_z_full_d_1_s_312;
    int __f2dace_SOA_ddxt_z_full_d_1_s;
  };
  union {
    int __f2dace_SOA_ddxt_z_full_d_2_s_313;
    int __f2dace_SOA_ddxt_z_full_d_2_s;
  };
  int __f2dace_SOA_deepatmo_divh_mc_d_0_s = {};
  int __f2dace_SOA_deepatmo_divzl_mc_d_0_s = {};
  int __f2dace_SOA_deepatmo_divzu_mc_d_0_s = {};
  union {
    int __f2dace_SOA_deepatmo_gradh_ifc_d_0_s_340;
    int __f2dace_SOA_deepatmo_gradh_ifc_d_0_s;
  };
  union {
    int __f2dace_SOA_deepatmo_gradh_mc_d_0_s_338;
    int __f2dace_SOA_deepatmo_gradh_mc_d_0_s;
  };
  union {
    int __f2dace_SOA_deepatmo_invr_ifc_d_0_s_341;
    int __f2dace_SOA_deepatmo_invr_ifc_d_0_s;
  };
  union {
    int __f2dace_SOA_deepatmo_invr_mc_d_0_s_339;
    int __f2dace_SOA_deepatmo_invr_mc_d_0_s;
  };
  int __f2dace_SOA_exner_exfac_d_0_s = {};
  int __f2dace_SOA_exner_exfac_d_1_s = {};
  int __f2dace_SOA_exner_exfac_d_2_s = {};
  int __f2dace_SOA_exner_ref_mc_d_0_s = {};
  int __f2dace_SOA_exner_ref_mc_d_1_s = {};
  int __f2dace_SOA_exner_ref_mc_d_2_s = {};
  int __f2dace_SOA_hmask_dd3d_d_0_s = {};
  int __f2dace_SOA_hmask_dd3d_d_1_s = {};
  int __f2dace_SOA_inv_ddqz_z_full_d_0_s = {};
  int __f2dace_SOA_inv_ddqz_z_full_d_1_s = {};
  int __f2dace_SOA_inv_ddqz_z_full_d_2_s = {};
  int __f2dace_SOA_pg_edgeblk_d_0_s = {};
  int __f2dace_SOA_pg_edgeidx_d_0_s = {};
  int __f2dace_SOA_pg_exdist_d_0_s = {};
  int __f2dace_SOA_pg_vertidx_d_0_s = {};
  int __f2dace_SOA_rayleigh_vn_d_0_s = {};
  int __f2dace_SOA_rayleigh_w_d_0_s = {};
  int __f2dace_SOA_rho_ref_mc_d_0_s = {};
  int __f2dace_SOA_rho_ref_mc_d_1_s = {};
  int __f2dace_SOA_rho_ref_mc_d_2_s = {};
  int __f2dace_SOA_rho_ref_me_d_0_s = {};
  int __f2dace_SOA_rho_ref_me_d_1_s = {};
  int __f2dace_SOA_rho_ref_me_d_2_s = {};
  int __f2dace_SOA_scalfac_dd3d_d_0_s = {};
  int __f2dace_SOA_theta_ref_ic_d_0_s = {};
  int __f2dace_SOA_theta_ref_ic_d_1_s = {};
  int __f2dace_SOA_theta_ref_ic_d_2_s = {};
  int __f2dace_SOA_theta_ref_mc_d_0_s = {};
  int __f2dace_SOA_theta_ref_mc_d_1_s = {};
  int __f2dace_SOA_theta_ref_mc_d_2_s = {};
  int __f2dace_SOA_theta_ref_me_d_0_s = {};
  int __f2dace_SOA_theta_ref_me_d_1_s = {};
  int __f2dace_SOA_theta_ref_me_d_2_s = {};
  int __f2dace_SOA_vertidx_gradp_d_0_s = {};
  int __f2dace_SOA_vertidx_gradp_d_1_s = {};
  int __f2dace_SOA_vertidx_gradp_d_2_s = {};
  int __f2dace_SOA_vertidx_gradp_d_3_s = {};
  int __f2dace_SOA_vwind_expl_wgt_d_0_s = {};
  int __f2dace_SOA_vwind_expl_wgt_d_1_s = {};
  int __f2dace_SOA_vwind_impl_wgt_d_0_s = {};
  int __f2dace_SOA_vwind_impl_wgt_d_1_s = {};
  union {
    int __f2dace_SOA_wgtfac_c_d_0_s_320;
    int __f2dace_SOA_wgtfac_c_d_0_s;
  };
  union {
    int __f2dace_SOA_wgtfac_c_d_1_s_321;
    int __f2dace_SOA_wgtfac_c_d_1_s;
  };
  union {
    int __f2dace_SOA_wgtfac_c_d_2_s_322;
    int __f2dace_SOA_wgtfac_c_d_2_s;
  };
  union {
    int __f2dace_SOA_wgtfac_e_d_0_s_323;
    int __f2dace_SOA_wgtfac_e_d_0_s;
  };
  union {
    int __f2dace_SOA_wgtfac_e_d_1_s_324;
    int __f2dace_SOA_wgtfac_e_d_1_s;
  };
  union {
    int __f2dace_SOA_wgtfac_e_d_2_s_325;
    int __f2dace_SOA_wgtfac_e_d_2_s;
  };
  int __f2dace_SOA_wgtfacq1_c_d_0_s = {};
  int __f2dace_SOA_wgtfacq1_c_d_1_s = {};
  int __f2dace_SOA_wgtfacq1_c_d_2_s = {};
  int __f2dace_SOA_wgtfacq_c_d_0_s = {};
  int __f2dace_SOA_wgtfacq_c_d_1_s = {};
  int __f2dace_SOA_wgtfacq_c_d_2_s = {};
  union {
    int __f2dace_SOA_wgtfacq_e_d_0_s_326;
    int __f2dace_SOA_wgtfacq_e_d_0_s;
  };
  union {
    int __f2dace_SOA_wgtfacq_e_d_1_s_327;
    int __f2dace_SOA_wgtfacq_e_d_1_s;
  };
  union {
    int __f2dace_SOA_wgtfacq_e_d_2_s_328;
    int __f2dace_SOA_wgtfacq_e_d_2_s;
  };
  int __f2dace_SOA_zdiff_gradp_d_0_s = {};
  int __f2dace_SOA_zdiff_gradp_d_1_s = {};
  int __f2dace_SOA_zdiff_gradp_d_2_s = {};
  int __f2dace_SOA_zdiff_gradp_d_3_s = {};
  int *bdy_mflx_e_blk = {};
  int bdy_mflx_e_dim = {};
  int *bdy_mflx_e_idx = {};
  double *coeff1_dwdz = {};
  double *coeff2_dwdz = {};
  double *coeff_gradekin = {};
  double *coeff_gradp = {};
  double *d2dexdz2_fac1_mc = {};
  double *d2dexdz2_fac1_mc_d_1_s = {};
  double *d2dexdz2_fac1_mc_d_2_s = {};
  double *d2dexdz2_fac2_mc = {};
  double *d2dexdz2_fac2_mc_d_1_s = {};
  double *d2dexdz2_fac2_mc_d_2_s = {};
  double *d_exner_dz_ref_ic = {};
  double *ddqz_z_full_e = {};
  double *ddqz_z_half = {};
  double *ddxn_z_full = {};
  double *ddxt_z_full = {};
  double *deepatmo_divh_mc = {};
  double *deepatmo_divzl_mc = {};
  double *deepatmo_divzu_mc = {};
  double *deepatmo_gradh_ifc = {};
  double *deepatmo_gradh_mc = {};
  double *deepatmo_invr_ifc = {};
  double *deepatmo_invr_mc = {};
  double *exner_exfac = {};
  double *exner_ref_mc = {};
  double *hmask_dd3d = {};
  double *inv_ddqz_z_full = {};
  int *pg_edgeblk = {};
  int *pg_edgeidx = {};
  double *pg_exdist = {};
  int pg_listdim = {};
  int *pg_vertidx = {};
  double *rayleigh_vn = {};
  double *rayleigh_w = {};
  double *rho_ref_mc = {};
  double *rho_ref_me = {};
  double *scalfac_dd3d = {};
  double *theta_ref_ic = {};
  double *theta_ref_mc = {};
  double *theta_ref_me = {};
  int *vertidx_gradp = {};
  double *vwind_expl_wgt = {};
  double *vwind_impl_wgt = {};
  double *wgtfac_c = {};
  double *wgtfac_e = {};
  double *wgtfacq1_c = {};
  double *wgtfacq_c = {};
  double *wgtfacq_e = {};
  double *zdiff_gradp = {};
};

struct t_prepare_adv {
  int __f2dace_SA_mass_flx_ic_d_0_s = {};
  int __f2dace_SA_mass_flx_ic_d_1_s = {};
  int __f2dace_SA_mass_flx_ic_d_2_s = {};
  int __f2dace_SA_mass_flx_me_d_0_s = {};
  int __f2dace_SA_mass_flx_me_d_1_s = {};
  int __f2dace_SA_mass_flx_me_d_2_s = {};
  int __f2dace_SA_vn_traj_d_0_s = {};
  int __f2dace_SA_vn_traj_d_1_s = {};
  int __f2dace_SA_vn_traj_d_2_s = {};
  int __f2dace_SA_vol_flx_ic_d_0_s = {};
  int __f2dace_SA_vol_flx_ic_d_1_s = {};
  int __f2dace_SA_vol_flx_ic_d_2_s = {};
  int __f2dace_SOA_mass_flx_ic_d_0_s = {};
  int __f2dace_SOA_mass_flx_ic_d_1_s = {};
  int __f2dace_SOA_mass_flx_ic_d_2_s = {};
  int __f2dace_SOA_mass_flx_me_d_0_s = {};
  int __f2dace_SOA_mass_flx_me_d_1_s = {};
  int __f2dace_SOA_mass_flx_me_d_2_s = {};
  int __f2dace_SOA_vn_traj_d_0_s = {};
  int __f2dace_SOA_vn_traj_d_1_s = {};
  int __f2dace_SOA_vn_traj_d_2_s = {};
  int __f2dace_SOA_vol_flx_ic_d_0_s = {};
  int __f2dace_SOA_vol_flx_ic_d_1_s = {};
  int __f2dace_SOA_vol_flx_ic_d_2_s = {};
  double *mass_flx_ic = {};
  double *mass_flx_me = {};
  double *vn_traj = {};
  double *vol_flx_ic = {};
};

struct t_nh_ref {
  int __f2dace_SA_vn_ref_d_0_s = {};
  int __f2dace_SA_vn_ref_d_1_s = {};
  int __f2dace_SA_vn_ref_d_2_s = {};
  int __f2dace_SA_w_ref_d_0_s = {};
  int __f2dace_SA_w_ref_d_1_s = {};
  int __f2dace_SA_w_ref_d_2_s = {};
  int __f2dace_SOA_vn_ref_d_0_s = {};
  int __f2dace_SOA_vn_ref_d_1_s = {};
  int __f2dace_SOA_vn_ref_d_2_s = {};
  int __f2dace_SOA_w_ref_d_0_s = {};
  int __f2dace_SOA_w_ref_d_1_s = {};
  int __f2dace_SOA_w_ref_d_2_s = {};
  double *vn_ref = {};
  double *w_ref = {};
};

struct t_nh_prog {
  int __f2dace_SA_exner_d_0_s = {};
  int __f2dace_SA_exner_d_1_s = {};
  int __f2dace_SA_exner_d_2_s = {};
  int __f2dace_SA_rho_d_0_s = {};
  int __f2dace_SA_rho_d_1_s = {};
  int __f2dace_SA_rho_d_2_s = {};
  int __f2dace_SA_theta_v_d_0_s = {};
  int __f2dace_SA_theta_v_d_1_s = {};
  int __f2dace_SA_theta_v_d_2_s = {};
  union {
    int __f2dace_SA_vn_d_0_s_288;
    int __f2dace_SA_vn_d_0_s;
  };
  union {
    int __f2dace_SA_vn_d_1_s_289;
    int __f2dace_SA_vn_d_1_s;
  };
  union {
    int __f2dace_SA_vn_d_2_s_290;
    int __f2dace_SA_vn_d_2_s;
  };
  union {
    int __f2dace_SA_w_d_0_s_285;
    int __f2dace_SA_w_d_0_s;
  };
  union {
    int __f2dace_SA_w_d_1_s_286;
    int __f2dace_SA_w_d_1_s;
  };
  union {
    int __f2dace_SA_w_d_2_s_287;
    int __f2dace_SA_w_d_2_s;
  };
  int __f2dace_SOA_exner_d_0_s = {};
  int __f2dace_SOA_exner_d_1_s = {};
  int __f2dace_SOA_exner_d_2_s = {};
  int __f2dace_SOA_rho_d_0_s = {};
  int __f2dace_SOA_rho_d_1_s = {};
  int __f2dace_SOA_rho_d_2_s = {};
  int __f2dace_SOA_theta_v_d_0_s = {};
  int __f2dace_SOA_theta_v_d_1_s = {};
  int __f2dace_SOA_theta_v_d_2_s = {};
  union {
    int __f2dace_SOA_vn_d_0_s_288;
    int __f2dace_SOA_vn_d_0_s;
  };
  union {
    int __f2dace_SOA_vn_d_1_s_289;
    int __f2dace_SOA_vn_d_1_s;
  };
  union {
    int __f2dace_SOA_vn_d_2_s_290;
    int __f2dace_SOA_vn_d_2_s;
  };
  union {
    int __f2dace_SOA_w_d_0_s_285;
    int __f2dace_SOA_w_d_0_s;
  };
  union {
    int __f2dace_SOA_w_d_1_s_286;
    int __f2dace_SOA_w_d_1_s;
  };
  union {
    int __f2dace_SOA_w_d_2_s_287;
    int __f2dace_SOA_w_d_2_s;
  };
  double *exner = {};
  double *rho = {};
  double *theta_v = {};
  double *vn = {};
  double *w = {};
};

struct t_nh_diag {
  int __f2dace_SA_ddt_exner_phy_d_0_s = {};
  int __f2dace_SA_ddt_exner_phy_d_1_s = {};
  int __f2dace_SA_ddt_exner_phy_d_2_s = {};
  int __f2dace_SA_ddt_vn_adv_d_0_s = {};
  int __f2dace_SA_ddt_vn_adv_d_1_s = {};
  int __f2dace_SA_ddt_vn_adv_d_2_s = {};
  union {
    int __f2dace_SA_ddt_vn_apc_pc_d_0_s_300;
    int __f2dace_SA_ddt_vn_apc_pc_d_0_s;
  };
  union {
    int __f2dace_SA_ddt_vn_apc_pc_d_1_s_301;
    int __f2dace_SA_ddt_vn_apc_pc_d_1_s;
  };
  union {
    int __f2dace_SA_ddt_vn_apc_pc_d_2_s_302;
    int __f2dace_SA_ddt_vn_apc_pc_d_2_s;
  };
  union {
    int __f2dace_SA_ddt_vn_apc_pc_d_3_s_303;
    int __f2dace_SA_ddt_vn_apc_pc_d_3_s;
  };
  int __f2dace_SA_ddt_vn_cor_d_0_s = {};
  int __f2dace_SA_ddt_vn_cor_d_1_s = {};
  int __f2dace_SA_ddt_vn_cor_d_2_s = {};
  int __f2dace_SA_ddt_vn_cor_pc_d_0_s = {};
  int __f2dace_SA_ddt_vn_cor_pc_d_1_s = {};
  int __f2dace_SA_ddt_vn_cor_pc_d_2_s = {};
  int __f2dace_SA_ddt_vn_cor_pc_d_3_s = {};
  int __f2dace_SA_ddt_vn_dmp_d_0_s = {};
  int __f2dace_SA_ddt_vn_dmp_d_1_s = {};
  int __f2dace_SA_ddt_vn_dmp_d_2_s = {};
  int __f2dace_SA_ddt_vn_dyn_d_0_s = {};
  int __f2dace_SA_ddt_vn_dyn_d_1_s = {};
  int __f2dace_SA_ddt_vn_dyn_d_2_s = {};
  int __f2dace_SA_ddt_vn_grf_d_0_s = {};
  int __f2dace_SA_ddt_vn_grf_d_1_s = {};
  int __f2dace_SA_ddt_vn_grf_d_2_s = {};
  int __f2dace_SA_ddt_vn_iau_d_0_s = {};
  int __f2dace_SA_ddt_vn_iau_d_1_s = {};
  int __f2dace_SA_ddt_vn_iau_d_2_s = {};
  int __f2dace_SA_ddt_vn_pgr_d_0_s = {};
  int __f2dace_SA_ddt_vn_pgr_d_1_s = {};
  int __f2dace_SA_ddt_vn_pgr_d_2_s = {};
  int __f2dace_SA_ddt_vn_phd_d_0_s = {};
  int __f2dace_SA_ddt_vn_phd_d_1_s = {};
  int __f2dace_SA_ddt_vn_phd_d_2_s = {};
  int __f2dace_SA_ddt_vn_phy_d_0_s = {};
  int __f2dace_SA_ddt_vn_phy_d_1_s = {};
  int __f2dace_SA_ddt_vn_phy_d_2_s = {};
  int __f2dace_SA_ddt_vn_ray_d_0_s = {};
  int __f2dace_SA_ddt_vn_ray_d_1_s = {};
  int __f2dace_SA_ddt_vn_ray_d_2_s = {};
  union {
    int __f2dace_SA_ddt_w_adv_pc_d_0_s_304;
    int __f2dace_SA_ddt_w_adv_pc_d_0_s;
  };
  union {
    int __f2dace_SA_ddt_w_adv_pc_d_1_s_305;
    int __f2dace_SA_ddt_w_adv_pc_d_1_s;
  };
  union {
    int __f2dace_SA_ddt_w_adv_pc_d_2_s_306;
    int __f2dace_SA_ddt_w_adv_pc_d_2_s;
  };
  union {
    int __f2dace_SA_ddt_w_adv_pc_d_3_s_307;
    int __f2dace_SA_ddt_w_adv_pc_d_3_s;
  };
  int __f2dace_SA_exner_dyn_incr_d_0_s = {};
  int __f2dace_SA_exner_dyn_incr_d_1_s = {};
  int __f2dace_SA_exner_dyn_incr_d_2_s = {};
  int __f2dace_SA_exner_incr_d_0_s = {};
  int __f2dace_SA_exner_incr_d_1_s = {};
  int __f2dace_SA_exner_incr_d_2_s = {};
  int __f2dace_SA_exner_pr_d_0_s = {};
  int __f2dace_SA_exner_pr_d_1_s = {};
  int __f2dace_SA_exner_pr_d_2_s = {};
  int __f2dace_SA_grf_bdy_mflx_d_0_s = {};
  int __f2dace_SA_grf_bdy_mflx_d_1_s = {};
  int __f2dace_SA_grf_bdy_mflx_d_2_s = {};
  int __f2dace_SA_grf_tend_mflx_d_0_s = {};
  int __f2dace_SA_grf_tend_mflx_d_1_s = {};
  int __f2dace_SA_grf_tend_mflx_d_2_s = {};
  int __f2dace_SA_grf_tend_rho_d_0_s = {};
  int __f2dace_SA_grf_tend_rho_d_1_s = {};
  int __f2dace_SA_grf_tend_rho_d_2_s = {};
  int __f2dace_SA_grf_tend_thv_d_0_s = {};
  int __f2dace_SA_grf_tend_thv_d_1_s = {};
  int __f2dace_SA_grf_tend_thv_d_2_s = {};
  int __f2dace_SA_grf_tend_vn_d_0_s = {};
  int __f2dace_SA_grf_tend_vn_d_1_s = {};
  int __f2dace_SA_grf_tend_vn_d_2_s = {};
  int __f2dace_SA_grf_tend_w_d_0_s = {};
  int __f2dace_SA_grf_tend_w_d_1_s = {};
  int __f2dace_SA_grf_tend_w_d_2_s = {};
  int __f2dace_SA_mass_fl_e_d_0_s = {};
  int __f2dace_SA_mass_fl_e_d_1_s = {};
  int __f2dace_SA_mass_fl_e_d_2_s = {};
  int __f2dace_SA_mass_fl_e_sv_d_0_s = {};
  int __f2dace_SA_mass_fl_e_sv_d_1_s = {};
  int __f2dace_SA_mass_fl_e_sv_d_2_s = {};
  int __f2dace_SA_mflx_ic_int_d_0_s = {};
  int __f2dace_SA_mflx_ic_int_d_1_s = {};
  int __f2dace_SA_mflx_ic_int_d_2_s = {};
  int __f2dace_SA_mflx_ic_ubc_d_0_s = {};
  int __f2dace_SA_mflx_ic_ubc_d_1_s = {};
  int __f2dace_SA_mflx_ic_ubc_d_2_s = {};
  int __f2dace_SA_rho_ic_d_0_s = {};
  int __f2dace_SA_rho_ic_d_1_s = {};
  int __f2dace_SA_rho_ic_d_2_s = {};
  int __f2dace_SA_rho_ic_int_d_0_s = {};
  int __f2dace_SA_rho_ic_int_d_1_s = {};
  int __f2dace_SA_rho_ic_int_d_2_s = {};
  int __f2dace_SA_rho_ic_ubc_d_0_s = {};
  int __f2dace_SA_rho_ic_ubc_d_1_s = {};
  int __f2dace_SA_rho_ic_ubc_d_2_s = {};
  int __f2dace_SA_rho_incr_d_0_s = {};
  int __f2dace_SA_rho_incr_d_1_s = {};
  int __f2dace_SA_rho_incr_d_2_s = {};
  int __f2dace_SA_theta_v_ic_d_0_s = {};
  int __f2dace_SA_theta_v_ic_d_1_s = {};
  int __f2dace_SA_theta_v_ic_d_2_s = {};
  int __f2dace_SA_theta_v_ic_int_d_0_s = {};
  int __f2dace_SA_theta_v_ic_int_d_1_s = {};
  int __f2dace_SA_theta_v_ic_int_d_2_s = {};
  int __f2dace_SA_theta_v_ic_ubc_d_0_s = {};
  int __f2dace_SA_theta_v_ic_ubc_d_1_s = {};
  int __f2dace_SA_theta_v_ic_ubc_d_2_s = {};
  union {
    int __f2dace_SA_vn_ie_d_0_s_294;
    int __f2dace_SA_vn_ie_d_0_s;
  };
  union {
    int __f2dace_SA_vn_ie_d_1_s_295;
    int __f2dace_SA_vn_ie_d_1_s;
  };
  union {
    int __f2dace_SA_vn_ie_d_2_s_296;
    int __f2dace_SA_vn_ie_d_2_s;
  };
  int __f2dace_SA_vn_ie_int_d_0_s = {};
  int __f2dace_SA_vn_ie_int_d_1_s = {};
  int __f2dace_SA_vn_ie_int_d_2_s = {};
  int __f2dace_SA_vn_ie_ubc_d_0_s = {};
  int __f2dace_SA_vn_ie_ubc_d_1_s = {};
  int __f2dace_SA_vn_ie_ubc_d_2_s = {};
  int __f2dace_SA_vn_incr_d_0_s = {};
  int __f2dace_SA_vn_incr_d_1_s = {};
  int __f2dace_SA_vn_incr_d_2_s = {};
  union {
    int __f2dace_SA_vt_d_0_s_291;
    int __f2dace_SA_vt_d_0_s;
  };
  union {
    int __f2dace_SA_vt_d_1_s_292;
    int __f2dace_SA_vt_d_1_s;
  };
  union {
    int __f2dace_SA_vt_d_2_s_293;
    int __f2dace_SA_vt_d_2_s;
  };
  union {
    int __f2dace_SA_w_concorr_c_d_0_s_297;
    int __f2dace_SA_w_concorr_c_d_0_s;
  };
  union {
    int __f2dace_SA_w_concorr_c_d_1_s_298;
    int __f2dace_SA_w_concorr_c_d_1_s;
  };
  union {
    int __f2dace_SA_w_concorr_c_d_2_s_299;
    int __f2dace_SA_w_concorr_c_d_2_s;
  };
  int __f2dace_SA_w_int_d_0_s = {};
  int __f2dace_SA_w_int_d_1_s = {};
  int __f2dace_SA_w_int_d_2_s = {};
  int __f2dace_SA_w_ubc_d_0_s = {};
  int __f2dace_SA_w_ubc_d_1_s = {};
  int __f2dace_SA_w_ubc_d_2_s = {};
  int __f2dace_SOA_ddt_exner_phy_d_0_s = {};
  int __f2dace_SOA_ddt_exner_phy_d_1_s = {};
  int __f2dace_SOA_ddt_exner_phy_d_2_s = {};
  int __f2dace_SOA_ddt_vn_adv_d_0_s = {};
  int __f2dace_SOA_ddt_vn_adv_d_1_s = {};
  int __f2dace_SOA_ddt_vn_adv_d_2_s = {};
  union {
    int __f2dace_SOA_ddt_vn_apc_pc_d_0_s_300;
    int __f2dace_SOA_ddt_vn_apc_pc_d_0_s;
  };
  union {
    int __f2dace_SOA_ddt_vn_apc_pc_d_1_s_301;
    int __f2dace_SOA_ddt_vn_apc_pc_d_1_s;
  };
  union {
    int __f2dace_SOA_ddt_vn_apc_pc_d_2_s_302;
    int __f2dace_SOA_ddt_vn_apc_pc_d_2_s;
  };
  union {
    int __f2dace_SOA_ddt_vn_apc_pc_d_3_s_303;
    int __f2dace_SOA_ddt_vn_apc_pc_d_3_s;
  };
  int __f2dace_SOA_ddt_vn_cor_d_0_s = {};
  int __f2dace_SOA_ddt_vn_cor_d_1_s = {};
  int __f2dace_SOA_ddt_vn_cor_d_2_s = {};
  int __f2dace_SOA_ddt_vn_cor_pc_d_0_s = {};
  int __f2dace_SOA_ddt_vn_cor_pc_d_1_s = {};
  int __f2dace_SOA_ddt_vn_cor_pc_d_2_s = {};
  int __f2dace_SOA_ddt_vn_cor_pc_d_3_s = {};
  int __f2dace_SOA_ddt_vn_dmp_d_0_s = {};
  int __f2dace_SOA_ddt_vn_dmp_d_1_s = {};
  int __f2dace_SOA_ddt_vn_dmp_d_2_s = {};
  int __f2dace_SOA_ddt_vn_dyn_d_0_s = {};
  int __f2dace_SOA_ddt_vn_dyn_d_1_s = {};
  int __f2dace_SOA_ddt_vn_dyn_d_2_s = {};
  int __f2dace_SOA_ddt_vn_grf_d_0_s = {};
  int __f2dace_SOA_ddt_vn_grf_d_1_s = {};
  int __f2dace_SOA_ddt_vn_grf_d_2_s = {};
  int __f2dace_SOA_ddt_vn_iau_d_0_s = {};
  int __f2dace_SOA_ddt_vn_iau_d_1_s = {};
  int __f2dace_SOA_ddt_vn_iau_d_2_s = {};
  int __f2dace_SOA_ddt_vn_pgr_d_0_s = {};
  int __f2dace_SOA_ddt_vn_pgr_d_1_s = {};
  int __f2dace_SOA_ddt_vn_pgr_d_2_s = {};
  int __f2dace_SOA_ddt_vn_phd_d_0_s = {};
  int __f2dace_SOA_ddt_vn_phd_d_1_s = {};
  int __f2dace_SOA_ddt_vn_phd_d_2_s = {};
  int __f2dace_SOA_ddt_vn_phy_d_0_s = {};
  int __f2dace_SOA_ddt_vn_phy_d_1_s = {};
  int __f2dace_SOA_ddt_vn_phy_d_2_s = {};
  int __f2dace_SOA_ddt_vn_ray_d_0_s = {};
  int __f2dace_SOA_ddt_vn_ray_d_1_s = {};
  int __f2dace_SOA_ddt_vn_ray_d_2_s = {};
  union {
    int __f2dace_SOA_ddt_w_adv_pc_d_0_s_304;
    int __f2dace_SOA_ddt_w_adv_pc_d_0_s;
  };
  union {
    int __f2dace_SOA_ddt_w_adv_pc_d_1_s_305;
    int __f2dace_SOA_ddt_w_adv_pc_d_1_s;
  };
  union {
    int __f2dace_SOA_ddt_w_adv_pc_d_2_s_306;
    int __f2dace_SOA_ddt_w_adv_pc_d_2_s;
  };
  union {
    int __f2dace_SOA_ddt_w_adv_pc_d_3_s_307;
    int __f2dace_SOA_ddt_w_adv_pc_d_3_s;
  };
  int __f2dace_SOA_exner_dyn_incr_d_0_s = {};
  int __f2dace_SOA_exner_dyn_incr_d_1_s = {};
  int __f2dace_SOA_exner_dyn_incr_d_2_s = {};
  int __f2dace_SOA_exner_incr_d_0_s = {};
  int __f2dace_SOA_exner_incr_d_1_s = {};
  int __f2dace_SOA_exner_incr_d_2_s = {};
  int __f2dace_SOA_exner_pr_d_0_s = {};
  int __f2dace_SOA_exner_pr_d_1_s = {};
  int __f2dace_SOA_exner_pr_d_2_s = {};
  int __f2dace_SOA_grf_bdy_mflx_d_0_s = {};
  int __f2dace_SOA_grf_bdy_mflx_d_1_s = {};
  int __f2dace_SOA_grf_bdy_mflx_d_2_s = {};
  int __f2dace_SOA_grf_tend_mflx_d_0_s = {};
  int __f2dace_SOA_grf_tend_mflx_d_1_s = {};
  int __f2dace_SOA_grf_tend_mflx_d_2_s = {};
  int __f2dace_SOA_grf_tend_rho_d_0_s = {};
  int __f2dace_SOA_grf_tend_rho_d_1_s = {};
  int __f2dace_SOA_grf_tend_rho_d_2_s = {};
  int __f2dace_SOA_grf_tend_thv_d_0_s = {};
  int __f2dace_SOA_grf_tend_thv_d_1_s = {};
  int __f2dace_SOA_grf_tend_thv_d_2_s = {};
  int __f2dace_SOA_grf_tend_vn_d_0_s = {};
  int __f2dace_SOA_grf_tend_vn_d_1_s = {};
  int __f2dace_SOA_grf_tend_vn_d_2_s = {};
  int __f2dace_SOA_grf_tend_w_d_0_s = {};
  int __f2dace_SOA_grf_tend_w_d_1_s = {};
  int __f2dace_SOA_grf_tend_w_d_2_s = {};
  int __f2dace_SOA_mass_fl_e_d_0_s = {};
  int __f2dace_SOA_mass_fl_e_d_1_s = {};
  int __f2dace_SOA_mass_fl_e_d_2_s = {};
  int __f2dace_SOA_mass_fl_e_sv_d_0_s = {};
  int __f2dace_SOA_mass_fl_e_sv_d_1_s = {};
  int __f2dace_SOA_mass_fl_e_sv_d_2_s = {};
  int __f2dace_SOA_mflx_ic_int_d_0_s = {};
  int __f2dace_SOA_mflx_ic_int_d_1_s = {};
  int __f2dace_SOA_mflx_ic_int_d_2_s = {};
  int __f2dace_SOA_mflx_ic_ubc_d_0_s = {};
  int __f2dace_SOA_mflx_ic_ubc_d_1_s = {};
  int __f2dace_SOA_mflx_ic_ubc_d_2_s = {};
  int __f2dace_SOA_rho_ic_d_0_s = {};
  int __f2dace_SOA_rho_ic_d_1_s = {};
  int __f2dace_SOA_rho_ic_d_2_s = {};
  int __f2dace_SOA_rho_ic_int_d_0_s = {};
  int __f2dace_SOA_rho_ic_int_d_1_s = {};
  int __f2dace_SOA_rho_ic_int_d_2_s = {};
  int __f2dace_SOA_rho_ic_ubc_d_0_s = {};
  int __f2dace_SOA_rho_ic_ubc_d_1_s = {};
  int __f2dace_SOA_rho_ic_ubc_d_2_s = {};
  int __f2dace_SOA_rho_incr_d_0_s = {};
  int __f2dace_SOA_rho_incr_d_1_s = {};
  int __f2dace_SOA_rho_incr_d_2_s = {};
  int __f2dace_SOA_theta_v_ic_d_0_s = {};
  int __f2dace_SOA_theta_v_ic_d_1_s = {};
  int __f2dace_SOA_theta_v_ic_d_2_s = {};
  int __f2dace_SOA_theta_v_ic_int_d_0_s = {};
  int __f2dace_SOA_theta_v_ic_int_d_1_s = {};
  int __f2dace_SOA_theta_v_ic_int_d_2_s = {};
  int __f2dace_SOA_theta_v_ic_ubc_d_0_s = {};
  int __f2dace_SOA_theta_v_ic_ubc_d_1_s = {};
  int __f2dace_SOA_theta_v_ic_ubc_d_2_s = {};
  union {
    int __f2dace_SOA_vn_ie_d_0_s_294;
    int __f2dace_SOA_vn_ie_d_0_s;
  };
  union {
    int __f2dace_SOA_vn_ie_d_1_s_295;
    int __f2dace_SOA_vn_ie_d_1_s;
  };
  union {
    int __f2dace_SOA_vn_ie_d_2_s_296;
    int __f2dace_SOA_vn_ie_d_2_s;
  };
  int __f2dace_SOA_vn_ie_int_d_0_s = {};
  int __f2dace_SOA_vn_ie_int_d_1_s = {};
  int __f2dace_SOA_vn_ie_int_d_2_s = {};
  int __f2dace_SOA_vn_ie_ubc_d_0_s = {};
  int __f2dace_SOA_vn_ie_ubc_d_1_s = {};
  int __f2dace_SOA_vn_ie_ubc_d_2_s = {};
  int __f2dace_SOA_vn_incr_d_0_s = {};
  int __f2dace_SOA_vn_incr_d_1_s = {};
  int __f2dace_SOA_vn_incr_d_2_s = {};
  union {
    int __f2dace_SOA_vt_d_0_s_291;
    int __f2dace_SOA_vt_d_0_s;
  };
  union {
    int __f2dace_SOA_vt_d_1_s_292;
    int __f2dace_SOA_vt_d_1_s;
  };
  union {
    int __f2dace_SOA_vt_d_2_s_293;
    int __f2dace_SOA_vt_d_2_s;
  };
  union {
    int __f2dace_SOA_w_concorr_c_d_0_s_297;
    int __f2dace_SOA_w_concorr_c_d_0_s;
  };
  union {
    int __f2dace_SOA_w_concorr_c_d_1_s_298;
    int __f2dace_SOA_w_concorr_c_d_1_s;
  };
  union {
    int __f2dace_SOA_w_concorr_c_d_2_s_299;
    int __f2dace_SOA_w_concorr_c_d_2_s;
  };
  int __f2dace_SOA_w_int_d_0_s = {};
  int __f2dace_SOA_w_int_d_1_s = {};
  int __f2dace_SOA_w_int_d_2_s = {};
  int __f2dace_SOA_w_ubc_d_0_s = {};
  int __f2dace_SOA_w_ubc_d_1_s = {};
  int __f2dace_SOA_w_ubc_d_2_s = {};
  double *ddt_exner_phy = {};
  double *ddt_vn_adv = {};
  int ddt_vn_adv_is_associated = {};
  double *ddt_vn_apc_pc = {};
  double *ddt_vn_cor = {};
  int ddt_vn_cor_is_associated = {};
  double *ddt_vn_cor_pc = {};
  double *ddt_vn_dmp = {};
  int ddt_vn_dmp_is_associated = {};
  double *ddt_vn_dyn = {};
  int ddt_vn_dyn_is_associated = {};
  double *ddt_vn_grf = {};
  int ddt_vn_grf_is_associated = {};
  double *ddt_vn_iau = {};
  int ddt_vn_iau_is_associated = {};
  double *ddt_vn_pgr = {};
  int ddt_vn_pgr_is_associated = {};
  double *ddt_vn_phd = {};
  int ddt_vn_phd_is_associated = {};
  double *ddt_vn_phy = {};
  double *ddt_vn_ray = {};
  int ddt_vn_ray_is_associated = {};
  double *ddt_w_adv_pc = {};
  double *exner_dyn_incr = {};
  double *exner_incr = {};
  double *exner_pr = {};
  double *grf_bdy_mflx = {};
  double *grf_tend_mflx = {};
  double *grf_tend_rho = {};
  double *grf_tend_thv = {};
  double *grf_tend_vn = {};
  double *grf_tend_w = {};
  double *mass_fl_e = {};
  double *mass_fl_e_sv = {};
  double max_vcfl_dyn = {};
  double *mflx_ic_int = {};
  double *mflx_ic_ubc = {};
  double *rho_ic = {};
  double *rho_ic_int = {};
  double *rho_ic_ubc = {};
  double *rho_incr = {};
  double *theta_v_ic = {};
  double *theta_v_ic_int = {};
  double *theta_v_ic_ubc = {};
  double *vn_ie = {};
  double *vn_ie_int = {};
  double *vn_ie_ubc = {};
  double *vn_incr = {};
  double *vt = {};
  double *w_concorr_c = {};
  double *w_int = {};
  double *w_ubc = {};
};

struct t_nh_state {
  int __f2dace_SA_prog_d_0_s = {};
  int __f2dace_SOA_prog_d_0_s = {};
  t_nh_diag *diag = {};
  t_nh_metrics *metrics = {};
  t_nh_prog **prog = {};
  t_nh_ref *ref = {};
};

struct t_grid_domain_decomp_info {
  union {
    int __f2dace_SA_owner_mask_d_0_s_2;
    int __f2dace_SA_owner_mask_d_0_s;
  };
  union {
    int __f2dace_SA_owner_mask_d_1_s_3;
    int __f2dace_SA_owner_mask_d_1_s;
  };
  union {
    int __f2dace_SOA_owner_mask_d_0_s_2;
    int __f2dace_SOA_owner_mask_d_0_s;
  };
  union {
    int __f2dace_SOA_owner_mask_d_1_s_3;
    int __f2dace_SOA_owner_mask_d_1_s;
  };
  int *owner_mask = {};
};

struct t_grid_cells {
  union {
    int __f2dace_SA_area_d_0_s_158;
    int __f2dace_SA_area_d_0_s;
  };
  union {
    int __f2dace_SA_area_d_1_s_159;
    int __f2dace_SA_area_d_1_s;
  };
  union {
    int __f2dace_SA_edge_blk_d_0_s_155;
    int __f2dace_SA_edge_blk_d_0_s;
  };
  union {
    int __f2dace_SA_edge_blk_d_1_s_156;
    int __f2dace_SA_edge_blk_d_1_s;
  };
  union {
    int __f2dace_SA_edge_blk_d_2_s_157;
    int __f2dace_SA_edge_blk_d_2_s;
  };
  union {
    int __f2dace_SA_edge_idx_d_0_s_152;
    int __f2dace_SA_edge_idx_d_0_s;
  };
  union {
    int __f2dace_SA_edge_idx_d_1_s_153;
    int __f2dace_SA_edge_idx_d_1_s;
  };
  union {
    int __f2dace_SA_edge_idx_d_2_s_154;
    int __f2dace_SA_edge_idx_d_2_s;
  };
  int __f2dace_SA_end_blk_d_0_s = {};
  int __f2dace_SA_end_blk_d_1_s = {};
  union {
    int __f2dace_SA_end_block_d_0_s_163;
    int __f2dace_SA_end_block_d_0_s;
  };
  union {
    int __f2dace_SA_end_index_d_0_s_161;
    int __f2dace_SA_end_index_d_0_s;
  };
  union {
    int __f2dace_SA_neighbor_blk_d_0_s_149;
    int __f2dace_SA_neighbor_blk_d_0_s;
  };
  union {
    int __f2dace_SA_neighbor_blk_d_1_s_150;
    int __f2dace_SA_neighbor_blk_d_1_s;
  };
  union {
    int __f2dace_SA_neighbor_blk_d_2_s_151;
    int __f2dace_SA_neighbor_blk_d_2_s;
  };
  union {
    int __f2dace_SA_neighbor_idx_d_0_s_146;
    int __f2dace_SA_neighbor_idx_d_0_s;
  };
  union {
    int __f2dace_SA_neighbor_idx_d_1_s_147;
    int __f2dace_SA_neighbor_idx_d_1_s;
  };
  union {
    int __f2dace_SA_neighbor_idx_d_2_s_148;
    int __f2dace_SA_neighbor_idx_d_2_s;
  };
  int __f2dace_SA_start_blk_d_0_s = {};
  int __f2dace_SA_start_blk_d_1_s = {};
  union {
    int __f2dace_SA_start_block_d_0_s_162;
    int __f2dace_SA_start_block_d_0_s;
  };
  union {
    int __f2dace_SA_start_index_d_0_s_160;
    int __f2dace_SA_start_index_d_0_s;
  };
  union {
    int __f2dace_SOA_area_d_0_s_158;
    int __f2dace_SOA_area_d_0_s;
  };
  union {
    int __f2dace_SOA_area_d_1_s_159;
    int __f2dace_SOA_area_d_1_s;
  };
  union {
    int __f2dace_SOA_edge_blk_d_0_s_155;
    int __f2dace_SOA_edge_blk_d_0_s;
  };
  union {
    int __f2dace_SOA_edge_blk_d_1_s_156;
    int __f2dace_SOA_edge_blk_d_1_s;
  };
  union {
    int __f2dace_SOA_edge_blk_d_2_s_157;
    int __f2dace_SOA_edge_blk_d_2_s;
  };
  union {
    int __f2dace_SOA_edge_idx_d_0_s_152;
    int __f2dace_SOA_edge_idx_d_0_s;
  };
  union {
    int __f2dace_SOA_edge_idx_d_1_s_153;
    int __f2dace_SOA_edge_idx_d_1_s;
  };
  union {
    int __f2dace_SOA_edge_idx_d_2_s_154;
    int __f2dace_SOA_edge_idx_d_2_s;
  };
  int __f2dace_SOA_end_blk_d_0_s = {};
  int __f2dace_SOA_end_blk_d_1_s = {};
  union {
    int __f2dace_SOA_end_block_d_0_s_163;
    int __f2dace_SOA_end_block_d_0_s;
  };
  union {
    int __f2dace_SOA_end_index_d_0_s_161;
    int __f2dace_SOA_end_index_d_0_s;
  };
  union {
    int __f2dace_SOA_neighbor_blk_d_0_s_149;
    int __f2dace_SOA_neighbor_blk_d_0_s;
  };
  union {
    int __f2dace_SOA_neighbor_blk_d_1_s_150;
    int __f2dace_SOA_neighbor_blk_d_1_s;
  };
  union {
    int __f2dace_SOA_neighbor_blk_d_2_s_151;
    int __f2dace_SOA_neighbor_blk_d_2_s;
  };
  union {
    int __f2dace_SOA_neighbor_idx_d_0_s_146;
    int __f2dace_SOA_neighbor_idx_d_0_s;
  };
  union {
    int __f2dace_SOA_neighbor_idx_d_1_s_147;
    int __f2dace_SOA_neighbor_idx_d_1_s;
  };
  union {
    int __f2dace_SOA_neighbor_idx_d_2_s_148;
    int __f2dace_SOA_neighbor_idx_d_2_s;
  };
  int __f2dace_SOA_start_blk_d_0_s = {};
  int __f2dace_SOA_start_blk_d_1_s = {};
  union {
    int __f2dace_SOA_start_block_d_0_s_162;
    int __f2dace_SOA_start_block_d_0_s;
  };
  union {
    int __f2dace_SOA_start_index_d_0_s_160;
    int __f2dace_SOA_start_index_d_0_s;
  };
  double *area = {};
  t_grid_domain_decomp_info *decomp_info = {};
  int *edge_blk = {};
  int *edge_idx = {};
  int *end_blk = {};
  int *end_block = {};
  int *end_index = {};
  int *neighbor_blk = {};
  int *neighbor_idx = {};
  int *start_blk = {};
  int *start_block = {};
  int *start_index = {};
};

struct t_int_state {
  union {
    int __f2dace_SA_c_lin_e_d_0_s_25;
    int __f2dace_SA_c_lin_e_d_0_s;
  };
  union {
    int __f2dace_SA_c_lin_e_d_1_s_26;
    int __f2dace_SA_c_lin_e_d_1_s;
  };
  union {
    int __f2dace_SA_c_lin_e_d_2_s_27;
    int __f2dace_SA_c_lin_e_d_2_s;
  };
  union {
    int __f2dace_SA_cells_aw_verts_d_0_s_31;
    int __f2dace_SA_cells_aw_verts_d_0_s;
  };
  union {
    int __f2dace_SA_cells_aw_verts_d_1_s_32;
    int __f2dace_SA_cells_aw_verts_d_1_s;
  };
  union {
    int __f2dace_SA_cells_aw_verts_d_2_s_33;
    int __f2dace_SA_cells_aw_verts_d_2_s;
  };
  union {
    int __f2dace_SA_e_bln_c_s_d_0_s_28;
    int __f2dace_SA_e_bln_c_s_d_0_s;
  };
  union {
    int __f2dace_SA_e_bln_c_s_d_1_s_29;
    int __f2dace_SA_e_bln_c_s_d_1_s;
  };
  union {
    int __f2dace_SA_e_bln_c_s_d_2_s_30;
    int __f2dace_SA_e_bln_c_s_d_2_s;
  };
  union {
    int __f2dace_SA_geofac_grdiv_d_0_s_37;
    int __f2dace_SA_geofac_grdiv_d_0_s;
  };
  union {
    int __f2dace_SA_geofac_grdiv_d_1_s_38;
    int __f2dace_SA_geofac_grdiv_d_1_s;
  };
  union {
    int __f2dace_SA_geofac_grdiv_d_2_s_39;
    int __f2dace_SA_geofac_grdiv_d_2_s;
  };
  union {
    int __f2dace_SA_geofac_n2s_d_0_s_43;
    int __f2dace_SA_geofac_n2s_d_0_s;
  };
  union {
    int __f2dace_SA_geofac_n2s_d_1_s_44;
    int __f2dace_SA_geofac_n2s_d_1_s;
  };
  union {
    int __f2dace_SA_geofac_n2s_d_2_s_45;
    int __f2dace_SA_geofac_n2s_d_2_s;
  };
  union {
    int __f2dace_SA_geofac_rot_d_0_s_40;
    int __f2dace_SA_geofac_rot_d_0_s;
  };
  union {
    int __f2dace_SA_geofac_rot_d_1_s_41;
    int __f2dace_SA_geofac_rot_d_1_s;
  };
  union {
    int __f2dace_SA_geofac_rot_d_2_s_42;
    int __f2dace_SA_geofac_rot_d_2_s;
  };
  union {
    int __f2dace_SA_rbf_vec_coeff_e_d_0_s_34;
    int __f2dace_SA_rbf_vec_coeff_e_d_0_s;
  };
  union {
    int __f2dace_SA_rbf_vec_coeff_e_d_1_s_35;
    int __f2dace_SA_rbf_vec_coeff_e_d_1_s;
  };
  union {
    int __f2dace_SA_rbf_vec_coeff_e_d_2_s_36;
    int __f2dace_SA_rbf_vec_coeff_e_d_2_s;
  };
  union {
    int __f2dace_SOA_c_lin_e_d_0_s_25;
    int __f2dace_SOA_c_lin_e_d_0_s;
  };
  union {
    int __f2dace_SOA_c_lin_e_d_1_s_26;
    int __f2dace_SOA_c_lin_e_d_1_s;
  };
  union {
    int __f2dace_SOA_c_lin_e_d_2_s_27;
    int __f2dace_SOA_c_lin_e_d_2_s;
  };
  union {
    int __f2dace_SOA_cells_aw_verts_d_0_s_31;
    int __f2dace_SOA_cells_aw_verts_d_0_s;
  };
  union {
    int __f2dace_SOA_cells_aw_verts_d_1_s_32;
    int __f2dace_SOA_cells_aw_verts_d_1_s;
  };
  union {
    int __f2dace_SOA_cells_aw_verts_d_2_s_33;
    int __f2dace_SOA_cells_aw_verts_d_2_s;
  };
  union {
    int __f2dace_SOA_e_bln_c_s_d_0_s_28;
    int __f2dace_SOA_e_bln_c_s_d_0_s;
  };
  union {
    int __f2dace_SOA_e_bln_c_s_d_1_s_29;
    int __f2dace_SOA_e_bln_c_s_d_1_s;
  };
  union {
    int __f2dace_SOA_e_bln_c_s_d_2_s_30;
    int __f2dace_SOA_e_bln_c_s_d_2_s;
  };
  union {
    int __f2dace_SOA_geofac_grdiv_d_0_s_37;
    int __f2dace_SOA_geofac_grdiv_d_0_s;
  };
  union {
    int __f2dace_SOA_geofac_grdiv_d_1_s_38;
    int __f2dace_SOA_geofac_grdiv_d_1_s;
  };
  union {
    int __f2dace_SOA_geofac_grdiv_d_2_s_39;
    int __f2dace_SOA_geofac_grdiv_d_2_s;
  };
  union {
    int __f2dace_SOA_geofac_n2s_d_0_s_43;
    int __f2dace_SOA_geofac_n2s_d_0_s;
  };
  union {
    int __f2dace_SOA_geofac_n2s_d_1_s_44;
    int __f2dace_SOA_geofac_n2s_d_1_s;
  };
  union {
    int __f2dace_SOA_geofac_n2s_d_2_s_45;
    int __f2dace_SOA_geofac_n2s_d_2_s;
  };
  union {
    int __f2dace_SOA_geofac_rot_d_0_s_40;
    int __f2dace_SOA_geofac_rot_d_0_s;
  };
  union {
    int __f2dace_SOA_geofac_rot_d_1_s_41;
    int __f2dace_SOA_geofac_rot_d_1_s;
  };
  union {
    int __f2dace_SOA_geofac_rot_d_2_s_42;
    int __f2dace_SOA_geofac_rot_d_2_s;
  };
  union {
    int __f2dace_SOA_rbf_vec_coeff_e_d_0_s_34;
    int __f2dace_SOA_rbf_vec_coeff_e_d_0_s;
  };
  union {
    int __f2dace_SOA_rbf_vec_coeff_e_d_1_s_35;
    int __f2dace_SOA_rbf_vec_coeff_e_d_1_s;
  };
  union {
    int __f2dace_SOA_rbf_vec_coeff_e_d_2_s_36;
    int __f2dace_SOA_rbf_vec_coeff_e_d_2_s;
  };
  double *c_lin_e = {};
  double *cells_aw_verts = {};
  double *e_bln_c_s = {};
  double *e_flx_avg = {};
  double *geofac_div = {};
  double *geofac_grdiv = {};
  double *geofac_grg = {};
  double *geofac_n2s = {};
  double *geofac_rot = {};
  double *nudgecoeff_e = {};
  double *pos_on_tplane_e = {};
  double *rbf_vec_coeff_e = {};
};

struct t_grid_vertices {
  union {
    int __f2dace_SA_cell_blk_d_0_s_167;
    int __f2dace_SA_cell_blk_d_0_s_203;
    int __f2dace_SA_cell_blk_d_0_s;
  };
  union {
    int __f2dace_SA_cell_blk_d_1_s_168;
    int __f2dace_SA_cell_blk_d_1_s_204;
    int __f2dace_SA_cell_blk_d_1_s;
  };
  union {
    int __f2dace_SA_cell_blk_d_2_s_169;
    int __f2dace_SA_cell_blk_d_2_s_205;
    int __f2dace_SA_cell_blk_d_2_s;
  };
  union {
    int __f2dace_SA_cell_idx_d_0_s_164;
    int __f2dace_SA_cell_idx_d_0_s_200;
    int __f2dace_SA_cell_idx_d_0_s;
  };
  union {
    int __f2dace_SA_cell_idx_d_1_s_165;
    int __f2dace_SA_cell_idx_d_1_s_201;
    int __f2dace_SA_cell_idx_d_1_s;
  };
  union {
    int __f2dace_SA_cell_idx_d_2_s_166;
    int __f2dace_SA_cell_idx_d_2_s_202;
    int __f2dace_SA_cell_idx_d_2_s;
  };
  union {
    int __f2dace_SA_edge_blk_d_0_s_155;
    int __f2dace_SA_edge_blk_d_0_s_209;
    int __f2dace_SA_edge_blk_d_0_s;
  };
  union {
    int __f2dace_SA_edge_blk_d_1_s_156;
    int __f2dace_SA_edge_blk_d_1_s_210;
    int __f2dace_SA_edge_blk_d_1_s;
  };
  union {
    int __f2dace_SA_edge_blk_d_2_s_157;
    int __f2dace_SA_edge_blk_d_2_s_211;
    int __f2dace_SA_edge_blk_d_2_s;
  };
  union {
    int __f2dace_SA_edge_idx_d_0_s_152;
    int __f2dace_SA_edge_idx_d_0_s_206;
    int __f2dace_SA_edge_idx_d_0_s;
  };
  union {
    int __f2dace_SA_edge_idx_d_1_s_153;
    int __f2dace_SA_edge_idx_d_1_s_207;
    int __f2dace_SA_edge_idx_d_1_s;
  };
  union {
    int __f2dace_SA_edge_idx_d_2_s_154;
    int __f2dace_SA_edge_idx_d_2_s_208;
    int __f2dace_SA_edge_idx_d_2_s;
  };
  union {
    int __f2dace_SA_end_block_d_0_s_163;
    int __f2dace_SA_end_block_d_0_s_199;
    int __f2dace_SA_end_block_d_0_s_215;
    int __f2dace_SA_end_block_d_0_s;
  };
  union {
    int __f2dace_SA_end_index_d_0_s_161;
    int __f2dace_SA_end_index_d_0_s_197;
    int __f2dace_SA_end_index_d_0_s_213;
    int __f2dace_SA_end_index_d_0_s;
  };
  union {
    int __f2dace_SA_start_block_d_0_s_162;
    int __f2dace_SA_start_block_d_0_s_198;
    int __f2dace_SA_start_block_d_0_s_214;
    int __f2dace_SA_start_block_d_0_s;
  };
  union {
    int __f2dace_SA_start_index_d_0_s_160;
    int __f2dace_SA_start_index_d_0_s_196;
    int __f2dace_SA_start_index_d_0_s_212;
    int __f2dace_SA_start_index_d_0_s;
  };
  union {
    int __f2dace_SOA_cell_blk_d_0_s_167;
    int __f2dace_SOA_cell_blk_d_0_s_203;
    int __f2dace_SOA_cell_blk_d_0_s;
  };
  union {
    int __f2dace_SOA_cell_blk_d_1_s_168;
    int __f2dace_SOA_cell_blk_d_1_s_204;
    int __f2dace_SOA_cell_blk_d_1_s;
  };
  union {
    int __f2dace_SOA_cell_blk_d_2_s_169;
    int __f2dace_SOA_cell_blk_d_2_s_205;
    int __f2dace_SOA_cell_blk_d_2_s;
  };
  union {
    int __f2dace_SOA_cell_idx_d_0_s_164;
    int __f2dace_SOA_cell_idx_d_0_s_200;
    int __f2dace_SOA_cell_idx_d_0_s;
  };
  union {
    int __f2dace_SOA_cell_idx_d_1_s_165;
    int __f2dace_SOA_cell_idx_d_1_s_201;
    int __f2dace_SOA_cell_idx_d_1_s;
  };
  union {
    int __f2dace_SOA_cell_idx_d_2_s_166;
    int __f2dace_SOA_cell_idx_d_2_s_202;
    int __f2dace_SOA_cell_idx_d_2_s;
  };
  union {
    int __f2dace_SOA_edge_blk_d_0_s_155;
    int __f2dace_SOA_edge_blk_d_0_s_209;
    int __f2dace_SOA_edge_blk_d_0_s;
  };
  union {
    int __f2dace_SOA_edge_blk_d_1_s_156;
    int __f2dace_SOA_edge_blk_d_1_s_210;
    int __f2dace_SOA_edge_blk_d_1_s;
  };
  union {
    int __f2dace_SOA_edge_blk_d_2_s_157;
    int __f2dace_SOA_edge_blk_d_2_s_211;
    int __f2dace_SOA_edge_blk_d_2_s;
  };
  union {
    int __f2dace_SOA_edge_idx_d_0_s_152;
    int __f2dace_SOA_edge_idx_d_0_s_206;
    int __f2dace_SOA_edge_idx_d_0_s;
  };
  union {
    int __f2dace_SOA_edge_idx_d_1_s_153;
    int __f2dace_SOA_edge_idx_d_1_s_207;
    int __f2dace_SOA_edge_idx_d_1_s;
  };
  union {
    int __f2dace_SOA_edge_idx_d_2_s_154;
    int __f2dace_SOA_edge_idx_d_2_s_208;
    int __f2dace_SOA_edge_idx_d_2_s;
  };
  union {
    int __f2dace_SOA_end_block_d_0_s_163;
    int __f2dace_SOA_end_block_d_0_s_199;
    int __f2dace_SOA_end_block_d_0_s_215;
    int __f2dace_SOA_end_block_d_0_s;
  };
  union {
    int __f2dace_SOA_end_index_d_0_s_161;
    int __f2dace_SOA_end_index_d_0_s_197;
    int __f2dace_SOA_end_index_d_0_s_213;
    int __f2dace_SOA_end_index_d_0_s;
  };
  union {
    int __f2dace_SOA_start_block_d_0_s_162;
    int __f2dace_SOA_start_block_d_0_s_198;
    int __f2dace_SOA_start_block_d_0_s_214;
    int __f2dace_SOA_start_block_d_0_s;
  };
  union {
    int __f2dace_SOA_start_index_d_0_s_160;
    int __f2dace_SOA_start_index_d_0_s_196;
    int __f2dace_SOA_start_index_d_0_s_212;
    int __f2dace_SOA_start_index_d_0_s;
  };
  int *cell_blk = {};
  int *cell_idx = {};
  int *edge_blk = {};
  int *edge_idx = {};
  int *end_block = {};
  int *end_index = {};
  int *start_block = {};
  int *start_index = {};
};

struct t_patch {
  t_grid_cells *cells = {};
  t_grid_edges *edges = {};
  int id = {};
  int n_childdom = {};
  int nblks_c = {};
  int nblks_e = {};
  int nblks_v = {};
  int nlev = {};
  int nlevp1 = {};
  int nshift = {};
  t_grid_vertices *verts = {};
};

using z_kin_hor_e_t = double;
using z_vt_ie_t = double;
using z_w_concorr_me_t = double;

#endif // __SHARED_STRUCT_DEFS_H__