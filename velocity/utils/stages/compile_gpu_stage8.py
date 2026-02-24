import argparse

import dace
from dace.transformation.passes import GPUKernelLaunchRestructure
from dace.transformation.passes.to_gpu import ToGPU
from dace import nodes
from dace.sdfg.sdfg import InterstateEdge
from dace.sdfg.state import SDFGState

import utils.stages.common as common
from utils.int64_to_int32 import int64_to_int32
from utils.pre_gpu_fixes import make_arrays_persistent
from utils.reassign_vars import reassign_vars
from utils.change_reduction_schedule import change_reduction_schedule
from utils.tile import tile_kernels
from utils.reshape_kernels import reshape_kernels, reshape_kernels_w_coarsening
from utils.hacky_cfl_clipping_related_kernel_removal import (
    hacky_cfl_clipping_related_kernel_removal,
)
from utils.decrease_bitwidth_of_const_arrays import (
    decrease_bitwidth_of_const_arrays,
    force_decrease_bitwidth_of_nblk_arrays,
)

from utils.profiling_patches import (
    insert_timers_for_profiling,
    insert_synchronization_for_profiling,
)

from utils.assignment_and_copy_kernel_to_memset_and_memcpy import (
    AssignmentAndCopyKernelToMemsetAndMemcpy,
)
from utils.create_profile_sdfg import create_profile_sdfg
from utils.pointwise_decompression import inject_pointwise_decompression
from utils.bfp_compression import inject_bfp_packing

STAGE_ID = 8

# Block Floating Point arrays — read-only, decoded inline via bfp_decode().
# These are excluded from inject_pointwise_decompression and handled by
# inject_bfp_packing (SDFG-level transformation) instead.
BFP_ARRAYS: list[str] = [
    "__CG_p_metrics__m_ddqz_z_half",
]

# Categorized arrays for FP32 conversion
GRID_METRICS = [
    "__CG_p_patch__CG_edges__m_inv_dual_edge_length",
    # "__CG_p_metrics__m_ddqz_z_half",  # moved to SENSITIVITY_CANDIDATES
    # "__CG_p_metrics__m_ddqz_z_full_e",
    # "__CG_p_metrics__m_wgtfac_e",      # moved to SENSITIVITY_RULED_OUT
    # "__CG_p_metrics__m_wgtfac_c",      # moved to SENSITIVITY_CANDIDATES
    "__CG_p_metrics__m_wgtfacq1_c",
    "__CG_p_metrics__m_wgtfacq_c",
    "__CG_p_metrics__m_wgtfacq_e",
    # "__CG_p_metrics__m_coeff1_dwdz",   # moved to SENSITIVITY_BORDERLINE
    # "__CG_p_metrics__m_coeff2_dwdz",   # moved to SENSITIVITY_CANDIDATES
    # "__CG_p_metrics__m_ddxn_z_full",   # moved to SENSITIVITY_CANDIDATES
    # "__CG_p_metrics__m_ddxt_z_full",   # moved to SENSITIVITY_RULED_OUT
    "__CG_p_metrics__m_coeff_gradp",
    "__CG_p_metrics__m_zdiff_gradp",
    # "__CG_p_metrics__m_coeff_gradekin",
    "__CG_p_patch__CG_edges__m_inv_primal_edge_length",
    # "__CG_p_metrics__m_inv_ddqz_z_full",  # not used in any SDFG variant
    "__CG_p_patch__CG_cells__m_area",
    # "__CG_p_patch__CG_edges__m_area_edge", # moved to SENSITIVITY_CANDIDATES
    "__CG_p_metrics__m_deepatmo_gradh_mc",
    "__CG_p_metrics__m_deepatmo_gradh_ifc",
    "__CG_p_metrics__m_deepatmo_invr_mc",
    "__CG_p_metrics__m_deepatmo_invr_ifc",
    "__CG_p_metrics__m_rayleigh_vn",
    "__CG_p_metrics__m_rayleigh_w",
    "__CG_p_metrics__m_d_exner_dz_ref_ic",
    "__CG_p_metrics__m_d2dexdz2_fac1_mc",
    "__CG_p_metrics__m_d2dexdz2_fac2_mc",
    "__CG_p_metrics__m_hmask_dd3d",
    "__CG_p_metrics__m_scalfac_dd3d",
]

INTERPOLATION_COEFFS = [
    # "__CG_p_int__m_rbf_vec_coeff_e",   # moved to SENSITIVITY_RULED_OUT
    # "__CG_p_int__m_e_bln_c_s",         # moved to SENSITIVITY_BORDERLINE
    # "__CG_p_int__m_c_lin_e",           # moved to SENSITIVITY_BORDERLINE
    # "__CG_p_int__m_geofac_grdiv",     # moved to SENSITIVITY_CANDIDATES
    # "__CG_p_int__m_geofac_rot",       # moved to SENSITIVITY_RULED_OUT
    "__CG_p_int__m_geofac_n2s",
    "__CG_p_int__m_cells_aw_verts",
    "__CG_p_int__m_e_flx_avg",
    # "__CG_p_patch__CG_edges__m_f_e",   # moved to SENSITIVITY_RULED_OUT
    "__CG_p_patch__CG_edges__m_fn_e",
    "__CG_p_patch__CG_edges__m_ft_e",
    "__CG_p_patch__CG_edges__m_tangent_orientation",
    "__CG_p_metrics__m_exner_exfac",
    "__CG_p_metrics__m_vwind_expl_wgt",
    "__CG_p_metrics__m_vwind_impl_wgt",
]

TRANSIENTS = [
    # "z_w_concorr_me",
    # "z_kin_hor_e",
    # "z_vt_ie",
    "z_v_grad_w",
    "z_w_v",
    # "zeta",
    # "z_ekinh",
    # "z_w_con_c_full",
    # "z_w_con_c",
    "z_w_concorr_mc",
    "z_th_ddz_exner_c",
    "z_dexner_dz_c",
    "z_gradh_exner",
    "z_rth_pr",
    "z_grad_rth",
    "z_graddiv_vn",
    "z_alpha",
    "z_beta",
    "z_q",
    "z_graddiv2_vn",
    "z_theta_v_pr_ic",
    "z_exner_ic",
    "z_flxdiv_mass",
    "z_flxdiv_theta",
    "z_hydro_corr",
    "z_dwdz_dd",
    "z_exner_ex_pr",
]

DIAGNOSTICS = [
    "__CG_p_diag__m_exner_pr",
    # "__CG_p_diag__m_vt",
    # "__CG_p_diag__m_vn_ie",
    "__CG_p_diag__m_mass_fl_e",
    "__CG_p_diag__m_mass_fl_e_sv",
    # "__CG_p_diag__m_w_concorr_c",     # moved to SENSITIVITY_BORDERLINE
    "__CG_p_diag__m_ddt_vn_apc_pc",
    "__CG_p_diag__m_ddt_w_adv_pc",
    "__CG_p_diag__m_rho_ic",
    "__CG_p_diag__m_theta_v_ic",
    "__CG_p_diag__m_exner_incr",
    "__CG_p_diag__m_rho_incr",
    "__CG_p_diag__m_vn_incr",
    "__CG_p_diag__m_exner_dyn_incr",
]

REFERENCE_STATES = [
    "__CG_p_metrics__m_exner_ref_mc",
    "__CG_p_metrics__m_rho_ref_mc",
    "__CG_p_metrics__m_theta_ref_mc",
    "__CG_p_metrics__m_rho_ref_me",
    "__CG_p_metrics__m_theta_ref_me",
    "__CG_p_metrics__m_theta_ref_ic",
    "__CG_p_ref__m_vn_ref",
    "__CG_p_ref__m_w_ref",
]

PREP_ADV = [
    "__CG_p_prep_adv__m_mass_flx_ic",
    "__CG_p_prep_adv__m_mass_flx_me",
    "__CG_p_prep_adv__m_vn_traj",
    "__CG_p_prep_adv__m_vol_flx_ic",
]

# Sensitivity-guided precision reduction workflow
# ------------------------------------------------
# Metric: Var(output delta) from Monte Carlo perturbation (10 samples,
#         per-element random U(-eps,+eps), eps=1e-5).
# Measured across 3 variants: lvn0_is1, lvn0_is2, lvn1_is1.
# Max Var(out) = worst case across all variants.
#
# Cutoff: RMS(out) < 1e-6  →  Var(out) < 1e-12  →  CANDIDATE
#         Otherwise         →  RULED_OUT
#
# Sensitivity is a cheap pre-filter; SNR is the actual gate.
SENSITIVITY_CANDIDATES: list[str] = [
    # max Var(out) < 1e-12 across all variants
    "__CG_p_int__m_c_lin_e",  # max Var = 1.6e-24
    "__CG_p_int__m_cells_aw_verts",  # max Var = 3.6e-25
    "__CG_p_metrics__m_coeff1_dwdz",  # max Var = 1.4e-27
    "__CG_p_metrics__m_coeff2_dwdz",  # max Var = 1.1e-28
    "__CG_p_patch__CG_edges__m_inv_dual_edge_length",  # max Var = 4.2e-29
    "__CG_p_patch__CG_edges__m_inv_primal_edge_length",  # max Var = 4.1e-29
    "__CG_p_patch__CG_edges__m_tangent_orientation",  # max Var = 4.0e-29
    "__CG_p_metrics__m_wgtfac_c",  # max Var = 5.5e-30
    "__CG_p_metrics__m_ddxt_z_full",  # max Var = 3.0e-26
    "__CG_p_metrics__m_ddxn_z_full",  # max Var = 0.0
    "__CG_p_metrics__m_ddqz_z_half",  # max Var = 0.0
    "__CG_p_patch__CG_edges__m_area_edge",  # max Var = 0.0
    "__CG_p_patch__CG_cells__m_area",  # max Var = 0.0
    "__CG_p_int__m_geofac_grdiv",  # max Var = 0.0
    "__CG_p_int__m_geofac_n2s",  # max Var = 0.0
    "__CG_p_diag__m_w_concorr_c",  # max Var = 1.7e-33
    "__CG_p_metrics__m_coeff_gradekin",  # max Var = 5.9e-20
]

SENSITIVITY_BORDERLINE: list[str] = [
    # Close to cutoff — hold for staged testing
    "__CG_p_int__m_e_bln_c_s",  # max Var = 3.9e-17
    "__CG_p_patch__CG_edges__m_f_e",  # max Var = 2.9e-17
    "__CG_p_int__m_geofac_rot",  # max Var = 2.5e-17
]

SENSITIVITY_RULED_OUT: list[str] = [
    # max Var(out) > 1e-12
    "__CG_p_int__m_rbf_vec_coeff_e",  # max Var = 1.1e-07
    "__CG_p_metrics__m_wgtfacq_e",  # max Var = 1.4e-12
    "__CG_p_metrics__m_wgtfac_e",  # max Var = 1.1e-13
]


def optimization_action(sdfg):
    """DEFINE THE OPTIMIZATION ACTION HERE"""
    # Pointwise decompression shim for inv_dual_edge_length
    # Run this FIRST before any other transformations to ensure
    # DaCe's analysis (like loop body generation) sees the updated types.
    inject_pointwise_decompression(
        sdfg,
        array_names=["__CG_p_patch__CG_edges__m_inv_dual_edge_length"],
        external_dtype=dace.float32,
    )

    if options.get("lower_all"):
        # Lower all float64 arrays in the SDFG
        candidates = [
            name
            for name, arr in sdfg.arrays.items()
            if hasattr(arr, "dtype") and arr.dtype == dace.float64
        ]

        if options["lowprec"].startswith("bfp"):
            # In BFP mode, strictly limit lowering to BFP-compatible arrays.
            # Everything else (transients, output structs) stays double.
            _OUTPUT_CG_PREFIXES = ("__CG_p_diag__", "__CG_p_prog__")
            array_names = [
                n
                for n in candidates
                if n.startswith("__CG_")
                and not any(n.startswith(p) for p in _OUTPUT_CG_PREFIXES)
            ]
            print(
                f"BFP Mode (_LOWER_ALL=1): Selected {len(array_names)} BFP-compatible arrays. "
                f"Skipping {len(candidates) - len(array_names)} transients/outputs to prevent FP16 fallback."
            )
        else:
            array_names = candidates
            print(
                f"Lowering all {len(array_names)} float64 arrays to {external_dtype} for pointwise decompression:\n{array_names}"
            )
    else:
        # Lower only sensitivity-validated safe fields
        array_names = list(SENSITIVITY_CANDIDATES)
        # # Lower the manually categorized lists (superseded by sensitivity)
        # array_names = (
        #     GRID_METRICS
        #     + INTERPOLATION_COEFFS
        #     + TRANSIENTS
        #     + DIAGNOSTICS
        #     + REFERENCE_STATES
        #     + PREP_ADV
        # )

    # Exclude BFP arrays from pointwise decompression — they get their own
    # SDFG-level transformation via inject_bfp_packing.
    bfp_set = set(BFP_ARRAYS) if options["lowprec"].startswith("bfp") else set()
    array_names = [n for n in array_names if n not in bfp_set]

    # Split arrays into four categories:
    #   boundary_cast_targets: __CG_ arrays with gpu_ sibling → boundary cast at H2D/D2H
    #     (CPU array stays double for serde, GPU gets float)
    #   gpu_transient: transient with gpu_ sibling but NOT __CG_ → change both dtypes
    #     (internal computation, no serde boundary)
    #   pure_transient: no gpu_ sibling → change dtype directly
    #   gpu_only: gpu_ prefixed with no bare counterpart → change dtype directly
    boundary_cast_targets = [
        n
        for n in array_names
        if n in sdfg.arrays and n.startswith("__CG_") and f"gpu_{n}" in sdfg.arrays
    ]
    transient = [
        n for n in array_names if n in sdfg.arrays and sdfg.arrays[n].transient
    ]
    gpu_only = [
        n
        for n in array_names
        if n in sdfg.arrays and n.startswith("gpu_") and n[4:] not in sdfg.arrays
    ]

    # gpu_ arrays whose bare counterpart exists are handled as siblings
    # by boundary_cast_targets (for __CG_) or gpu_transient (for z_*)
    gpu_sibling = [
        n
        for n in array_names
        if n in sdfg.arrays and n.startswith("gpu_") and n[4:] in sdfg.arrays
    ]

    # Verify categories are mutually exclusive and cover all arrays
    all_categorized = (
        set(boundary_cast_targets)
        | set(gpu_transient)
        | set(pure_transient)
        | set(gpu_only)
        | set(gpu_sibling)
    )
    assert len(all_categorized) == len(boundary_cast_targets) + len(
        gpu_transient
    ) + len(pure_transient) + len(gpu_only) + len(gpu_sibling), (
        f"Overlapping categories detected"
    )
    uncategorized = [
        n for n in array_names if n in sdfg.arrays and n not in all_categorized
    ]
    assert not uncategorized, f"Arrays not in any category: {uncategorized}"

    if non_transient:
        inject_pointwise_decompression(
            sdfg,
            array_names=non_transient,
            external_dtype=external_dtype,
        )
    if transient and can_skip_shims:
        print(
            f"Lowering {len(transient)} transient arrays without shims (native {external_dtype}): {transient}"
        )
        inject_pointwise_decompression(
            sdfg,
            array_names=transient,
            external_dtype=external_dtype,
            skip_shims=True,
        )
    elif transient:
        inject_pointwise_decompression(
            sdfg,
            array_names=transient,
            external_dtype=external_dtype,
        )
        for name in pure_transient:
            _propagate_dtype(sdfg, name, external_dtype)
    if gpu_only:
        # GPU-only arrays (no CPU counterpart — created by ToGPU pass):
        # change dtype directly.
        print(
            f"Lowering {len(gpu_only)} GPU-only arrays to native {external_dtype}: {gpu_only}"
        )
        for name in gpu_only:
            _propagate_dtype(sdfg, name, external_dtype)

    # Lower float64 scalars used in GPU computation.
    # CFL-related scalars stay double (they feed the CFL reduction which is excluded).
    _CFL_SCALARS = {
        "cfl_w_limit",
        "max_vcfl_dyn_var_152",
        "__CG_p_diag__m_max_vcfl_dyn",
        "tmp_call_1",
        "tmp_call_18",
    }
    if options.get("lower_all"):
        scalars_to_lower = [
            name
            for name, arr in sdfg.arrays.items()
            if isinstance(arr, dace.data.Scalar)
            and arr.dtype == dace.float64
            and name not in _CFL_SCALARS
        ]
        if scalars_to_lower:
            print(
                f"Lowering {len(scalars_to_lower)} float64 scalars to {external_dtype}: {scalars_to_lower}"
            )
            for name in scalars_to_lower:
                _propagate_dtype(sdfg, name, external_dtype)

    # Lower transient double scalars inside nested SDFGs (GPU kernel
    # intermediates like difcoef, tmp_arg_*, w_con_e, etc.).
    # These are not visible at the top level — _propagate_dtype doesn't
    # reach them. Only maxvcfl must stay double (feeds CFL reduction).
    _CFL_NESTED_SCALARS = {"maxvcfl"}
    if options.get("lower_all"):
        for nsdfg in sdfg.all_sdfgs_recursive():
            if nsdfg is sdfg:
                continue
            for name, arr in list(nsdfg.arrays.items()):
                if (
                    isinstance(arr, dace.data.Scalar)
                    and arr.dtype == dace.float64
                    and arr.transient
                    and name not in _CFL_NESTED_SCALARS
                    and name not in _CFL_SCALARS
                ):
                    arr.dtype = external_dtype

    # BFP packing: change GPU arrays to uint8[packed_size], insert CPU-side
    # pack Map, replace H2D edge with packed version.
    if options["lowprec"].startswith("bfp") and BFP_ARRAYS:
        bfp_mantissa_bits = {"bfp8": 8, "bfp16": 16, "bfp32": 16}[options["lowprec"]]
        inject_bfp_packing(sdfg, BFP_ARRAYS, mantissa_bits=bfp_mantissa_bits)

    # Apply transformations
    gpu_levmask_desc = sdfg.arrays.get("gpu_levmask")
    print(
        "gpu_levmask shape:",
        gpu_levmask_desc.shape,
        "strides:",
        gpu_levmask_desc.strides,
    )
    """
    PermuteArrayDimensions(
        permute_map={"gpu_levmask": [1, 0]},
        add_permute_maps=False,
    ).apply_pass(sdfg=sdfg, pipeline_results={})
    """
    inverse_strides(sdfg, "gpu_levmask")
    sdfg.validate()
    gpu_levmask_desc = sdfg.arrays.get("gpu_levmask")
    print(
        "gpu_levmask new shape:",
        gpu_levmask_desc.shape,
        "new strides:",
        gpu_levmask_desc.strides,
    )
    # raise Exception("DEBUG: PermuteArrayDimensions applied, check gpu_levmask shape and strides")

    do_reduce_bitwidth = os.getenv("_REDUCE_BITWIDTH_TRANSFORMATION", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    if do_reduce_bitwidth:
        # nproma dependent ones
        sdfg = decrease_bitwidth_of_const_arrays(
            sdfg,
            array_names={
                "gpu___CG_p_patch__CG_cells__m_edge_idx",
                # "gpu___CG_p_patch__CG_cells__m_end_index", #CPU Only
                "gpu___CG_p_patch__CG_cells__m_neighbor_idx",
                # "gpu___CG_p_patch__CG_cells__m_start_index", #CPU Only
                "gpu___CG_p_patch__CG_edges__m_cell_idx",
                # "gpu___CG_p_patch__CG_edges__m_end_index", #CPU Only
                "gpu___CG_p_patch__CG_edges__m_quad_idx",
                # "gpu___CG_p_patch__CG_edges__m_start_index", #CPU Only
                "gpu___CG_p_patch__CG_edges__m_vertex_idx",
                "gpu___CG_p_patch__CG_verts__m_cell_idx",
                "gpu___CG_p_patch__CG_verts__m_edge_idx",
                # "gpu___CG_p_patch__CG_verts__m_end_index", #CPU Only
                # "gpu___CG_p_patch__CG_verts__m_start_index", #CPU Only
            },
            nproma_name="__CG_global_data__m_nproma",
        )
        # nlock dependent ones
        sdfg = force_decrease_bitwidth_of_nblk_arrays(
            sdfg,
            multi_val_array_names={
                # "gpu___CG_p_patch__CG_cells__m_neighbor_blk", #1
                "gpu___CG_p_patch__CG_cells__m_edge_blk",  # 1,2
                # "gpu___CG_p_patch__CG_edges__m_cell_blk", #1
                "gpu___CG_p_patch__CG_edges__m_quad_blk",  # 1,2
                "gpu___CG_p_patch__CG_edges__m_neighbor_blk",  # 1,2
                # "gpu___CG_p_patch__CG_edges__m_vertex_blk", #1
                # "gpu___CG_p_patch__CG_verts__m_cell_blk", #1
                "gpu___CG_p_patch__CG_verts__m_edge_blk",  # 1,2
                # "gpu___CG_p_patch__CG_edges__m_neighbor_blk", #1
            },
            single_val_array_names={
                "gpu___CG_p_patch__CG_cells__m_neighbor_blk",  # 1
                # "gpu___CG_p_patch__CG_cells__m_edge_blk", #1,2
                "gpu___CG_p_patch__CG_edges__m_cell_blk",  # 1
                # "gpu___CG_p_patch__CG_edges__m_quad_blk", #1,2
                # "gpu___CG_p_patch__CG_edges__m_neighbor_blk", #1,2
                "gpu___CG_p_patch__CG_edges__m_vertex_blk",  # 1
                "gpu___CG_p_patch__CG_verts__m_cell_blk",  # 1
                # "gpu___CG_p_patch__CG_verts__m_edge_blk", #1,2
                "gpu___CG_p_patch__CG_edges__m_neighbor_blk",  # 1
            },
        )

    # vertex_blk, cell_blk, cell_neighbor_blk, vertex_neighbor_blk are always 1 if nblocks_c is 1
    # if nblocks_c is 1 then edge_start_block, edge_end_block, edge_blk are 1 or 2 (can do uint8)
    # Force these variables to have lower bitwidths, always

    int64_to_int32(sdfg)
    # start_index and end_index are between [1, nproma] -> ~20k in our data, ~200k in some other cases int16 is -32768, 32767
    # start_block and end_blocks are between [0, nblks] -> usually 1 or 2 as we pass nblocks_c for the science config
    # Force start and end blks to int8?
    # TODO: force start_block and end_block to int8

    # Rm copy/memset kernels with API calls
    AssignmentAndCopyKernelToMemsetAndMemcpy().apply_pass(sdfg, {})

    # TODO: GPU read-write has unit size of 32-bits, uint8_t won't help unless we tile
    # Assigning a warp to the column is not a very good idea
    # reshape_kernels(sdfg, True)
    # Must be individualized for each kernel
    do_tile = os.getenv("_TILE", "0").lower() in ("1", "true", "yes")
    if do_tile:
        x_coarsening = int(os.environ.get("X_COARSENING", 1))
        y_coarsening = int(os.environ.get("Y_COARSENING", 1))
        x_block_size = int(os.environ.get("X_BLOCK_SIZE", 256))
        y_block_size = int(os.environ.get("Y_BLOCK_SIZE", 1))
        y_unroll_factor = int(os.environ.get("Y_UNROLL_FACTOR", 1))
        # reshape_kernels(sdfg)
        reshape_kernels_w_coarsening(
            sdfg,
            x_coarsening=x_coarsening,
            y_coarsening=y_coarsening,
            x_block_size=x_block_size,
            y_block_size=y_block_size,
            unroll_x=True,
            unroll_x_factor=None,
            unroll_y=True,
            unroll_y_factor=y_unroll_factor,
        )
    # tile_kernels(sdfg)
    # sdfg.simplify()
    # Sync first
    insert_synchronization_for_profiling(sdfg)
    insert_timers_for_profiling(sdfg)
    # set_default_stream(sdfg)
    sdfg.validate()

    do_profile = os.getenv("_PROFILE", "0").lower() in ("1", "true", "yes")
    if do_profile:
        create_profile_sdfg(sdfg)

    return sdfg


def main():
    common.standard_main(
        STAGE_ID,
        optimization_action,
    )


if __name__ == "__main__":
    main()
