#ifndef __STORAGE_TYPES_H__
#define __STORAGE_TYPES_H__

// Define storage types for read-only arrays to allow controllable compression.
// Defaults to double (FP64) if not specified via -DLOW_PREC_TYPE during compilation.

#ifndef LOW_PREC_TYPE
#define LOW_PREC_TYPE double
#endif

// Metrics (All in GRID_METRICS or INTERPOLATION_COEFFS lists in Stage 8 script)
using inv_dual_edge_length_storage_t = LOW_PREC_TYPE;
using inv_primal_edge_length_storage_t = LOW_PREC_TYPE;
using area_edge_storage_t = LOW_PREC_TYPE;
using f_e_storage_t = LOW_PREC_TYPE;
using fn_e_storage_t = LOW_PREC_TYPE;
using ft_e_storage_t = LOW_PREC_TYPE;
using tangent_orientation_storage_t = LOW_PREC_TYPE;
using area_storage_t = LOW_PREC_TYPE;
using ddqz_z_half_storage_t = LOW_PREC_TYPE;
// using ddqz_z_full_e_storage_t = LOW_PREC_TYPE;
using ddqz_z_full_e_storage_t = double;
using wgtfac_e_storage_t = LOW_PREC_TYPE;
using wgtfac_c_storage_t = LOW_PREC_TYPE;
using wgtfacq1_c_storage_t = LOW_PREC_TYPE;
using wgtfacq_c_storage_t = LOW_PREC_TYPE;
using wgtfacq_e_storage_t = LOW_PREC_TYPE;
using coeff1_dwdz_storage_t = LOW_PREC_TYPE;
using coeff2_dwdz_storage_t = LOW_PREC_TYPE;
using ddxn_z_full_storage_t = LOW_PREC_TYPE;
using ddxt_z_full_storage_t = LOW_PREC_TYPE;
using coeff_gradp_storage_t = LOW_PREC_TYPE;
using zdiff_gradp_storage_t = LOW_PREC_TYPE;
// using coeff_gradekin_storage_t = LOW_PREC_TYPE;
using coeff_gradekin_storage_t = double;
using inv_ddqz_z_full_storage_t = LOW_PREC_TYPE;
using deepatmo_gradh_mc_storage_t = LOW_PREC_TYPE;
using deepatmo_gradh_ifc_storage_t = LOW_PREC_TYPE;
using deepatmo_invr_mc_storage_t = LOW_PREC_TYPE;
using deepatmo_invr_ifc_storage_t = LOW_PREC_TYPE;
using rayleigh_vn_storage_t = LOW_PREC_TYPE;
using rayleigh_w_storage_t = LOW_PREC_TYPE;
using d_exner_dz_ref_ic_storage_t = LOW_PREC_TYPE;
using d2dexdz2_fac1_mc_storage_t = LOW_PREC_TYPE;
using d2dexdz2_fac2_mc_storage_t = LOW_PREC_TYPE;
using hmask_dd3d_storage_t = LOW_PREC_TYPE;
using scalfac_dd3d_storage_t = LOW_PREC_TYPE;

// Interpolation (All in INTERPOLATION_COEFFS list in Stage 8 script)
using rbf_vec_coeff_e_storage_t = LOW_PREC_TYPE;
using e_bln_c_s_storage_t = LOW_PREC_TYPE;
using c_lin_e_storage_t = LOW_PREC_TYPE;
using geofac_grdiv_storage_t = LOW_PREC_TYPE;
using geofac_rot_storage_t = LOW_PREC_TYPE;
using geofac_n2s_storage_t = LOW_PREC_TYPE;
using cells_aw_verts_storage_t = LOW_PREC_TYPE;
using e_flx_avg_storage_t = LOW_PREC_TYPE;
using exner_exfac_storage_t = LOW_PREC_TYPE;
using vwind_expl_wgt_storage_t = LOW_PREC_TYPE;
using vwind_impl_wgt_storage_t = LOW_PREC_TYPE;

// Diagnostics (Subset in DIAGNOSTICS list in Stage 8 script)
using exner_pr_storage_t = LOW_PREC_TYPE;
// using vt_storage_t = LOW_PREC_TYPE;
using vt_storage_t = double;
// using vn_ie_storage_t = LOW_PREC_TYPE;
using vn_ie_storage_t = double;
using mass_fl_e_storage_t = LOW_PREC_TYPE;
using mass_fl_e_sv_storage_t = LOW_PREC_TYPE;
using w_concorr_c_storage_t = LOW_PREC_TYPE;
using ddt_vn_apc_pc_storage_t = LOW_PREC_TYPE;
using ddt_w_adv_pc_storage_t = LOW_PREC_TYPE;
using rho_ic_storage_t = LOW_PREC_TYPE;
using theta_v_ic_storage_t = LOW_PREC_TYPE;
using exner_incr_storage_t = LOW_PREC_TYPE;
using rho_incr_storage_t = LOW_PREC_TYPE;
using vn_incr_storage_t = LOW_PREC_TYPE;
using exner_dyn_incr_storage_t = LOW_PREC_TYPE;

// These are NOT in the Stage 8 script conversion lists and must remain FP64
using ddt_exner_phy_storage_t = double;
using ddt_vn_adv_storage_t = double;
using ddt_vn_cor_storage_t = double;
using ddt_vn_cor_pc_storage_t = double;
using ddt_vn_dmp_storage_t = double;
using ddt_vn_dyn_storage_t = double;
using ddt_vn_grf_storage_t = double;
using ddt_vn_iau_storage_t = double;
using ddt_vn_pgr_storage_t = double;
using ddt_vn_phd_storage_t = double;
using ddt_vn_phy_storage_t = double;
using ddt_vn_ray_storage_t = double;
using grf_bdy_mflx_storage_t = double;
using grf_tend_mflx_storage_t = double;
using grf_tend_rho_storage_t = double;
using grf_tend_thv_storage_t = double;
using grf_tend_vn_storage_t = double;
using grf_tend_w_storage_t = double;
using mflx_ic_int_storage_t = double;
using mflx_ic_ubc_storage_t = double;
using rho_ic_int_storage_t = double;
using rho_ic_ubc_storage_t = double;
using theta_v_ic_int_storage_t = double;
using theta_v_ic_ubc_storage_t = double;
using vn_ie_int_storage_t = double;
using vn_ie_ubc_storage_t = double;
using w_int_storage_t = double;
using w_ubc_storage_t = double;

// Prognostics (NOT in Stage 8 script conversion lists; must remain FP64)
using exner_storage_t = double;
using rho_storage_t = double;
using theta_v_storage_t = double;
using vn_storage_t = double;
using w_storage_t = double;

// Reference States (All in REFERENCE_STATES list in Stage 8 script)
using exner_ref_mc_storage_t = LOW_PREC_TYPE;
using rho_ref_mc_storage_t = LOW_PREC_TYPE;
using theta_ref_mc_storage_t = LOW_PREC_TYPE;
using rho_ref_me_storage_t = LOW_PREC_TYPE;
using theta_ref_me_storage_t = LOW_PREC_TYPE;
using theta_ref_ic_storage_t = LOW_PREC_TYPE;
using vn_ref_storage_t = LOW_PREC_TYPE;
using w_ref_storage_t = LOW_PREC_TYPE;

// Advection Prep (All in PREP_ADV list in Stage 8 script)
using mass_flx_ic_storage_t = LOW_PREC_TYPE;
using mass_flx_me_storage_t = LOW_PREC_TYPE;
using vn_traj_storage_t = LOW_PREC_TYPE;
using vol_flx_ic_storage_t = LOW_PREC_TYPE;

// Transients (Designated for FP32 in Stage 8 script)
// using z_kin_hor_e_storage_t = LOW_PREC_TYPE;
using z_kin_hor_e_storage_t = double;
// using z_vt_ie_storage_t = LOW_PREC_TYPE;
using z_vt_ie_storage_t = double;
// using z_w_concorr_me_storage_t = LOW_PREC_TYPE;
using z_w_concorr_me_storage_t = double;

#endif // __STORAGE_TYPES_H__
