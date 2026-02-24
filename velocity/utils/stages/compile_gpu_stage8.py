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

# Dynamic list of arrays that were BFP-packed during optimization.
# Populated by optimize() at runtime; consumed by compile_if_propagated_sdfgs.py
# for text-level patching. Empty until optimize() runs.
BFP_PACKED: list[str] = []

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


def coarsen_coalescable_dim(sdfg: dace.SDFG, factor: int = 2):
    """Thread coarsening: for each multi-dim map, find the coalescable
    dimension (the loop var that indexes the first array dim
    most often), tile it by `factor`, and unroll the inner map."""
    tile_targets = []
    for entry, state in sdfg.all_nodes_recursive():
        if not isinstance(entry, nodes.MapEntry) or len(entry.map.params) < 2:
            continue

        # Rule 1: skip already coarsened (has 'tile_' in params)
        if any(p.startswith("tile_") for p in entry.map.params):
            continue

        # Rule 2: innermost only
        is_innermost = True
        for node in state.scope_children()[entry]:
            if isinstance(node, nodes.MapEntry):
                is_innermost = False
                break
        if not is_innermost:
            continue

        # Rule 3: winner selection
        first_counts = {p: 0 for p in entry.map.params}
        any_counts = {p: 0 for p in entry.map.params}

        # Count occurrences in first and any dimension
        scope_subgraph = state.scope_subgraph(entry)
        for edge in scope_subgraph.edges():
            if (
                not isinstance(edge.data, dace.Memlet)
                or edge.data.is_empty()
                or edge.data.subset is None
            ):
                continue

            subset = edge.data.subset
            # Check first component
            try:
                first_dim = subset[0]
                first_idx = (
                    first_dim[0] if isinstance(first_dim, (list, tuple)) else first_dim
                )
                f_syms = {str(s) for s in first_idx.free_symbols}
                for p in entry.map.params:
                    if p in f_syms:
                        first_counts[p] += 1
            except (AttributeError, IndexError, TypeError):
                pass

            # Count any component
            try:
                all_syms = {str(s) for s in subset.free_symbols}
                for p in entry.map.params:
                    if p in all_syms:
                        any_counts[p] += 1
            except AttributeError:
                pass

        # Selection logic: priority to first dimension
        if any(c > 0 for c in first_counts.values()):
            winner = max(first_counts, key=lambda p: first_counts[p])
        elif any(c > 0 for c in any_counts.values()):
            winner = max(any_counts, key=lambda p: any_counts[p])
        else:
            # Fallback: pick the first parameter if no usage is detected
            winner = entry.map.params[0]

        winner_idx = list(entry.map.params).index(winner)
        tile_sizes = [1] * len(entry.map.params)
        tile_sizes[winner_idx] = factor
        tile_targets.append((state.parent, state, entry, tuple(tile_sizes), winner))

    print(f"Thread coarsening (×{factor} + unroll) on {len(tile_targets)} maps")
    for nsdfg, state, entry, tile_sizes, winner in tile_targets:
        print(f"  {entry.map.label}: coarsen '{winner}', tile_sizes={tile_sizes}")
        MapTiling.apply_to(
            nsdfg,
            map_entry=entry,
            options={"tile_sizes": tile_sizes},
        )

    # Mark inner tiled maps as Sequential + unroll so CUDA codegen
    # emits them as #pragma unroll loops inside the kernel.
    for nsdfg in sdfg.all_sdfgs_recursive():
        for state in nsdfg.states():
            for node in state.nodes():
                if not isinstance(node, nodes.MapEntry):
                    continue
                if any(p.startswith("tile_") for p in node.map.params):
                    continue
                if node.map.unroll:
                    continue
                for s, e, st in node.map.range:
                    range_syms = set()
                    for expr in (s, e, st):
                        try:
                            range_syms |= {str(x) for x in expr.free_symbols}
                        except AttributeError:
                            pass
                    if any(x.startswith("tile_") for x in range_syms):
                        node.map.schedule = dace.ScheduleType.Sequential
                        node.map.unroll = True
                        break


def optimization_action(sdfg):
    """DEFINE THE OPTIMIZATION ACTION HERE"""
    # Pointwise decompression shim for inv_dual_edge_length
    # Run this FIRST before any other transformations to ensure
    # DaCe's analysis (like loop body generation) sees the updated types.
    lowprec_str = os.getenv("_LOWPREC", "fp64").lower()
    lowprec_map = {
        "fp64": dace.float64,
        "fp32": dace.float32,
        "f32": dace.float32,
        "f64": dace.float64,
        "f16": dace.float16,
        "bfp16": dace.float16,
        "bfp32": dace.float32,
    }
    if options["lowprec"] not in lowprec_map:
        raise ValueError(
            f"Unknown lowprec value: {options['lowprec']!r}. "
            f"Valid options: {', '.join(lowprec_map)}"
        )
    external_dtype = lowprec_map[options["lowprec"]]

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

    # BFP exclusion is now dynamic — handled after boundary_cast_targets are
    # classified as read-only vs read-write (see bfp_targets below).

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

    # For BFP modes, route read-only __CG_ boundary_cast_targets to BFP
    # packing instead of boundary cast. Output structs (p_diag, p_prog) and
    # non-__CG_ function params are read-write and stay with boundary cast.
    _OUTPUT_CG_PREFIXES = ("__CG_p_diag__", "__CG_p_prog__")
    bfp_targets = []
    print(
        f"DEBUG BFP: lowprec={options['lowprec']!r}, "
        f"boundary_cast_targets ({len(boundary_cast_targets)}): {boundary_cast_targets}"
    )
    if options["lowprec"].startswith("bfp") and boundary_cast_targets:
        for name in boundary_cast_targets:
            is_readonly = name.startswith("__CG_") and not any(
                name.startswith(p) for p in _OUTPUT_CG_PREFIXES
            )
            if is_readonly:
                bfp_targets.append(name)
        if bfp_targets:
            print(
                f"BFP: {len(bfp_targets)} read-only arrays will be BFP-packed"
                f" (out of {len(boundary_cast_targets)} boundary_cast_targets)"
            )
            boundary_cast_targets = [
                n for n in boundary_cast_targets if n not in set(bfp_targets)
            ]

    if boundary_cast_targets:
        inject_boundary_cast(
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
    if options["lowprec"].startswith("bfp") and bfp_targets:
        bfp_mantissa_bits = {"bfp8": 8, "bfp16": 16, "bfp32": 16}[options["lowprec"]]
        inject_bfp_packing(sdfg, bfp_targets, mantissa_bits=bfp_mantissa_bits)
        # TAG THE ARRAYS: mark them so the compiler knows they are BFP
        for name in bfp_targets:
            gpu_name = f"gpu_{name}"
            if gpu_name in sdfg.arrays:
                sdfg.arrays[gpu_name].debuginfo = dace.dtypes.DebugInfo(
                    start_line=0, end_line=0, filename="BFP_PACKED"
                )
        # Store for text-level patching in compile_if_propagated_sdfgs.py
        global BFP_PACKED
        BFP_PACKED = list(bfp_targets)

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
    if not (options["lowprec"].startswith("bfp") and bfp_targets):
        sdfg.validate()  # skip when BFP — nested SDFG descriptors mismatch

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

        if options["permute_dimensions"]:
            # When vertical dim is contiguous, we want 32 threads in X to handle levels.
            # We fill the rest of the 1024-thread block with 32 horizontal elements in Y.
            x_block_size = 32
            y_block_size = 32
            # Update launch bounds for the entire SDFG
            from utils.reshape_kernels import update_gpu_block_size

            update_gpu_block_size(sdfg, [32, 32, 1])

        print(
            f"Reshaping kernels with x_block_size={x_block_size}, y_block_size={y_block_size}"
        )
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
            permute_dims=options["permute_dimensions"],
        )
    # tile_kernels(sdfg)
    # sdfg.simplify()
    # Sync first
    insert_synchronization_for_profiling(sdfg)
    insert_timers_for_profiling(sdfg)
    if not (options["lowprec"].startswith("bfp") and bfp_targets):
        sdfg.validate()  # skip when BFP — nested SDFG descriptors mismatch

    do_profile = os.getenv("_PROFILE", "0").lower() in ("1", "true", "yes")
    if do_profile:
        create_profile_sdfg(sdfg)

    # Floatify: replace double literals and math functions in tasklets with
    # float equivalents to prevent FP64 promotion on GPU.
    # Done last so it sees final tasklet code after tiling/coarsening.
    if options["lowprec"] != "fp64":
        import re

        for nsdfg in sdfg.all_sdfgs_recursive():
            for state in nsdfg.states():
                for node in state.nodes():
                    if not isinstance(node, nodes.Tasklet):
                        continue

                    code = node.code.as_string
                    new_code = code

                    # Target constants
                    constants = ["0.5", "0.85", "1.0", "0.0", "0.05", "0.65", "1.15"]

                    if node.language == dace.Language.CPP:
                        # For C++, use the 'f' suffix (e.g., 0.5f)
                        for val in constants:
                            new_code = re.sub(
                                rf"(?<![0-9fF]){re.escape(val)}(?![0-9fF])",
                                f"{val}f",
                                new_code,
                            )
                            new_code = re.sub(
                                rf"(?<![0-9fF])\-{re.escape(val)}(?![0-9fF])",
                                f"-{val}f",
                                new_code,
                            )
                        # abs() -> fabsf()
                        new_code = re.sub(r"\babs\(", "fabsf(", new_code)
                        # ipow(x, 2) -> (x * x)
                        new_code = re.sub(
                            r"dace::math::ipow\(([^,]+),\s*2\)",
                            r"((\1) * (\1))",
                            new_code,
                        )
                    else:
                        # For Python tasklets, use float() wrapper.
                        # DaCe parses Python AST so 'f' suffix is invalid;
                        # float(0.5) emits float(0.5) in C++ — compile-time constant.
                        for val in constants:
                            new_code = re.sub(
                                rf"(?<![0-9fF]){re.escape(val)}(?![0-9fF])",
                                f"float({val})",
                                new_code,
                            )
                            new_code = re.sub(
                                rf"(?<![0-9fF])\-{re.escape(val)}(?![0-9fF])",
                                f"float(-{val})",
                                new_code,
                            )
                        # abs() is already type-preserving in C++ (std::abs
                        # overloads for float/double/etc.), no change needed.
                        # x ** 2 -> (x * x)  (Python tasklets use ** operator,
                        # DaCe codegen converts to dace::math::ipow which stays fp64)
                        new_code = re.sub(
                            r"(\w+)\s*\*\*\s*2\b",
                            r"((\1) * (\1))",
                            new_code,
                        )

                    if new_code != code:
                        node.code = dace.properties.CodeBlock(new_code, node.language)

    # Disabled for now. Performance actually degrades.
    # coarsen_coalescable_dim(sdfg, factor=2)

    return sdfg, bfp_targets


def main():
    common.standard_main(
        STAGE_ID,
        optimization_action,
    )


if __name__ == "__main__":
    main()
