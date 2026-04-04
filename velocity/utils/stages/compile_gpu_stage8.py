import threading
import argparse
import os
from pathlib import Path
import dace
import re
from utils.permute_array_dimensions import inverse_strides
from dace.transformation.passes import GPUKernelLaunchRestructure
from dace.transformation.passes.to_gpu import ToGPU
from dace import nodes
from dace.sdfg.sdfg import InterstateEdge
from dace.sdfg.state import SDFGState
from dace.transformation.dataflow.tiling import MapTiling

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
    remove_profiling_states,
    remove_sync_states,
    insert_program_entry_exit_syncs,
    rm_redundant_copies,
)
from utils.change_flatten_lib_to_shallow_copy import change_flatten_lib_to_shallow_copy
from utils.input_to_gpu import input_to_gpu
from utils.make_flattened_data_to_input import (
    make_flattened_data_to_non_transient_gpu_input as _make_flat_gpu_input,
)
from utils.add_set_zero import add_set_zero

from utils.assignment_and_copy_kernel_to_memset_and_memcpy import (
    AssignmentAndCopyKernelToMemsetAndMemcpy,
)
from utils.create_profile_sdfg import create_profile_sdfg
from utils.boundary_cast import inject_boundary_cast, inject_gpu_boundary_cast, _propagate_dtype
from utils.bfp_compression import inject_bfp_packing

STAGE_ID = 8
WORKLOG_FILE = "stage8_lowering.log"

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

def downcast_to_minimal_bitwidth(sdfg: dace.SDFG):
    """
    Downcasts large integer arrays to 16-bit where possible based on
    domain-specific knowledge (nproma/nblocks).
    """
    # nproma dependent ones
    sdfg = decrease_bitwidth_of_const_arrays(
        sdfg,
        array_names={
            "gpu___CG_p_patch__CG_cells__m_edge_idx",
            "gpu___CG_p_patch__CG_cells__m_neighbor_idx",
            "gpu___CG_p_patch__CG_edges__m_cell_idx",
            "gpu___CG_p_patch__CG_edges__m_quad_idx",
            "gpu___CG_p_patch__CG_edges__m_vertex_idx",
            "gpu___CG_p_patch__CG_verts__m_cell_idx",
            "gpu___CG_p_patch__CG_verts__m_edge_idx",
        },
        nproma_name="__CG_global_data__m_nproma",
    )
    # nlock dependent ones
    sdfg = force_decrease_bitwidth_of_nblk_arrays(
        sdfg,
        multi_val_array_names={
            "gpu___CG_p_patch__CG_cells__m_edge_blk",
            "gpu___CG_p_patch__CG_edges__m_quad_blk",
            "gpu___CG_p_patch__CG_edges__m_neighbor_blk",
            "gpu___CG_p_patch__CG_verts__m_edge_blk",
        },
        single_val_array_names={
            "gpu___CG_p_patch__CG_cells__m_neighbor_blk",
            "gpu___CG_p_patch__CG_edges__m_cell_blk",
            "gpu___CG_p_patch__CG_edges__m_vertex_blk",
            "gpu___CG_p_patch__CG_verts__m_cell_blk",
            "gpu___CG_p_patch__CG_edges__m_neighbor_blk",
        },
    )
    return sdfg


def coarsen_coalescable_dim(sdfg: dace.SDFG, factor: int = 2):
    """Thread coarsening: for each multi-dim map, find the coalescable
    dimension (the loop var that indexes the last/contiguous array dim
    most often), tile it by `factor`, and unroll the inner map."""
    tile_targets = []
    for nsdfg in sdfg.all_sdfgs_recursive():
        for state in nsdfg.states():
            for entry in state.nodes():
                if not isinstance(entry, nodes.MapEntry):
                    continue
                if len(entry.map.params) < 2:
                    continue

                # Count how often each param appears in the last subset dim
                param_count = {p: 0 for p in entry.map.params}
                total = 0
                for edge in state.edges():
                    if not isinstance(edge.src, nodes.Tasklet) and not isinstance(
                        edge.dst, nodes.Tasklet
                    ):
                        continue
                    if edge.data.is_empty() or edge.data.subset is None:
                        continue
                    if len(edge.data.subset) < 2:
                        continue
                    last_start = edge.data.subset[-1][0]
                    total += 1
                    syms = {str(s) for s in last_start.free_symbols}
                    for p in entry.map.params:
                        if p in syms:
                            param_count[p] += 1

                if total == 0:
                    continue
                winner = max(param_count, key=lambda p: param_count[p])
                if param_count[winner] < total * 0.5:
                    continue  # no clear coalescable dimension
                winner_idx = list(entry.map.params).index(winner)
                tile_sizes = [1] * len(entry.map.params)
                tile_sizes[winner_idx] = factor
                tile_targets.append((nsdfg, state, entry, tuple(tile_sizes), winner))

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


def downcast_to_minimal_bitwidth(sdfg: dace.SDFG):
    """
    Downcasts large integer arrays to 16-bit where possible based on
    domain-specific knowledge (nproma/nblocks).
    """
    # nproma dependent ones
    sdfg = decrease_bitwidth_of_const_arrays(
        sdfg,
        array_names={
            "gpu___CG_p_patch__CG_cells__m_edge_idx",
            "gpu___CG_p_patch__CG_cells__m_neighbor_idx",
            "gpu___CG_p_patch__CG_edges__m_cell_idx",
            "gpu___CG_p_patch__CG_edges__m_quad_idx",
            "gpu___CG_p_patch__CG_edges__m_vertex_idx",
            "gpu___CG_p_patch__CG_verts__m_cell_idx",
            "gpu___CG_p_patch__CG_verts__m_edge_idx",
        },
        nproma_name="__CG_global_data__m_nproma",
    )
    # nlock dependent ones
    sdfg = force_decrease_bitwidth_of_nblk_arrays(
        sdfg,
        multi_val_array_names={
            "gpu___CG_p_patch__CG_cells__m_edge_blk",
            "gpu___CG_p_patch__CG_edges__m_quad_blk",
            "gpu___CG_p_patch__CG_edges__m_neighbor_blk",
            "gpu___CG_p_patch__CG_verts__m_edge_blk",
        },
        single_val_array_names={
            "gpu___CG_p_patch__CG_cells__m_neighbor_blk",
            "gpu___CG_p_patch__CG_edges__m_cell_blk",
            "gpu___CG_p_patch__CG_edges__m_vertex_blk",
            "gpu___CG_p_patch__CG_verts__m_cell_blk",
            "gpu___CG_p_patch__CG_edges__m_neighbor_blk",
        },
    )
    return sdfg


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


def _write_worklog(
    sdfg_name: str,
    options: dict,
    external_dtype,
    boundary_cast_targets: list[str],
    gpu_transient: list[str],
    pure_transient: list[str],
    gpu_only: list[str],
    gpu_sibling: list[str],
    bfp_targets: list[str],
    scalars_lowered: list[str],
    scalars_excluded: list[str],
    nested_scalars_lowered: int,
    nested_scalars_to_int32: int,
    nested_scalars_excluded: list[str],
    arrays_not_lowered: list[tuple[str, str]] | None = None,
    scalars_not_lowered: list[tuple[str, str]] | None = None,
    fp64_expected: list[tuple[str, str, str]] | None = None,
    fp64_unexpected: list[tuple[str, str]] | None = None,
):
    """Append lowering report for one SDFG variant to the worklog file."""
    import fcntl

    path = Path(WORKLOG_FILE)
    mode = "a" if path.exists() else "w"

    with open(path, mode) as f:
        fcntl.flock(f, fcntl.LOCK_EX)

        if f.tell() == 0:
            f.write("=" * 72 + "\n")
            f.write(f"Stage 8 Lowering Report\n")
            f.write(f"lowprec={options['lowprec']}  lower_all={options.get('lower_all')}"
                    f"  reduce_bitwidth={options.get('reduce_bitwidth')}"
                    f"  permute={options.get('permute_dimensions')}\n")
            f.write("=" * 72 + "\n\n")

        f.write("-" * 72 + "\n")
        f.write(f"SDFG: {sdfg_name}  ->  {external_dtype}\n")
        f.write("-" * 72 + "\n")

        total_lowered = (len(boundary_cast_targets) + len(gpu_transient)
                         + len(pure_transient) + len(gpu_only))
        f.write(f"\nArrays lowered: {total_lowered}\n")

        if boundary_cast_targets:
            f.write(f"\n  Boundary-cast ({len(boundary_cast_targets)}):\n")
            for n in sorted(boundary_cast_targets):
                f.write(f"    {n}\n")
        if gpu_transient:
            f.write(f"\n  GPU transient ({len(gpu_transient)}):\n")
            for n in sorted(gpu_transient):
                f.write(f"    {n}\n")
        if pure_transient:
            f.write(f"\n  Pure transient ({len(pure_transient)}):\n")
            for n in sorted(pure_transient):
                f.write(f"    {n}\n")
        if gpu_only:
            f.write(f"\n  GPU-only ({len(gpu_only)}):\n")
            for n in sorted(gpu_only):
                f.write(f"    {n}\n")

        if gpu_sibling:
            f.write(f"\n  GPU sibling (handled via boundary-cast, {len(gpu_sibling)}):\n")
            for n in sorted(gpu_sibling):
                f.write(f"    {n}\n")

        if bfp_targets:
            f.write(f"\n  BFP-packed ({len(bfp_targets)}):\n")
            for n in sorted(bfp_targets):
                f.write(f"    {n}\n")

        f.write(f"\nScalars lowered: {len(scalars_lowered)}\n")
        if scalars_lowered:
            for n in sorted(scalars_lowered):
                f.write(f"    {n}\n")
        if scalars_excluded:
            f.write(f"\n  Scalars excluded (CFL): {len(scalars_excluded)}\n")
            for n in sorted(scalars_excluded):
                f.write(f"    {n}\n")

        if nested_scalars_lowered or nested_scalars_to_int32:
            f.write(f"\nNested SDFG scalars: {nested_scalars_lowered} lowered,"
                    f" {nested_scalars_to_int32} -> int32 (comparison)\n")
        if nested_scalars_excluded:
            f.write(f"  Nested excluded (CFL): {', '.join(sorted(nested_scalars_excluded))}\n")

        if arrays_not_lowered:
            f.write(f"\nArrays NOT lowered ({len(arrays_not_lowered)}):\n")
            # Group by reason
            by_reason: dict[str, list[str]] = {}
            for name, reason in arrays_not_lowered:
                by_reason.setdefault(reason, []).append(name)
            for reason in sorted(by_reason):
                names = sorted(by_reason[reason])
                f.write(f"\n  [{reason}] ({len(names)}):\n")
                for n in names:
                    f.write(f"    {n}\n")

        if scalars_not_lowered:
            f.write(f"\nScalars NOT lowered ({len(scalars_not_lowered)}):\n")
            by_reason: dict[str, list[str]] = {}
            for name, reason in scalars_not_lowered:
                by_reason.setdefault(reason, []).append(name)
            for reason in sorted(by_reason):
                names = sorted(by_reason[reason])
                f.write(f"\n  [{reason}] ({len(names)}):\n")
                for n in names:
                    f.write(f"    {n}\n")

        n_expected = len(fp64_expected) if fp64_expected else 0
        n_unexpected = len(fp64_unexpected) if fp64_unexpected else 0
        if n_expected or n_unexpected:
            f.write(f"\nPost-lowering fp64 audit: "
                    f"{n_expected} expected, {n_unexpected} UNEXPECTED\n")

        if fp64_unexpected:
            f.write(f"\n  *** UNEXPECTED fp64 ({n_unexpected}) ***\n")
            by_loc: dict[str, list[str]] = {}
            for loc, entry in fp64_unexpected:
                by_loc.setdefault(loc, []).append(entry)
            for loc in sorted(by_loc):
                items = sorted(set(by_loc[loc]))
                f.write(f"\n    {loc} ({len(items)}):\n")
                for item in items:
                    f.write(f"      {item}\n")

        if fp64_expected:
            f.write(f"\n  Expected fp64 ({n_expected}):\n")
            by_reason: dict[str, list[str]] = {}
            for loc, entry, reason in fp64_expected:
                by_reason.setdefault(reason, []).append(f"{entry}  [{loc}]")
            for reason in sorted(by_reason):
                items = sorted(set(by_reason[reason]))
                f.write(f"\n    [{reason}] ({len(items)}):\n")
                for item in items:
                    f.write(f"      {item}\n")

        f.write("\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def optimization_action(sdfg):
    """DEFINE THE OPTIMIZATION ACTION HERE"""
    # Pointwise decompression shim for inv_dual_edge_length
    # Run this FIRST before any other transformations to ensure
    # DaCe's analysis (like loop body generation) sees the updated types.
    options = common.get_build_options()
    lowprec_map = {
        "fp64": dace.float64,
        "fp32": dace.float32,
        "f32": dace.float32,
        "f64": dace.float64,
        "f16": dace.float16,
        "bfp16": dace.float16,
        "bfp32": dace.float32,
    }
    external_dtype = lowprec_map.get(options["lowprec"], dace.float64)

    # Arrays that input_to_gpu will make GPU-resident in integration mode.
    # Must be excluded from lowering/boundary-cast to avoid schedule conflicts.
    _INTEGRATION_GPU_INPUTS = {"z_w_concorr_me", "z_kin_hor_e", "z_vt_ie"}
    _INTEGRATION_EXCLUDE = set()
    if options["build_for_integration"]:
        for n in _INTEGRATION_GPU_INPUTS:
            _INTEGRATION_EXCLUDE.add(n)
            _INTEGRATION_EXCLUDE.add(f"gpu_{n}")

    if options.get("lower_all"):
        # CFL reduction arrays managed by change_reduction_schedule.py.
        # These cross the GPU/CPU boundary in ways that
        # inject_pointwise_decompression cannot handle — CPU cast shims
        # would dereference GPU device pointers.
        _CFL_EXCLUDE = {
            "gpu_maxvcfl_arr",
            "gpu_vcflmax",
            "vcflmax",
            "maxvcfl",
        }

        # Lower all float64 arrays in the SDFG
        array_names = [
            name
            for name, arr in sdfg.arrays.items()
            if isinstance(arr, dace.data.Array)
            and arr.dtype == dace.float64
            and arr.total_size != 1
            and name not in _CFL_EXCLUDE
            and name not in _INTEGRATION_EXCLUDE
        ]
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
    bfp_set = set(BFP_ARRAYS)
    array_names = [n for n in array_names if n not in bfp_set]

    # Split arrays into three categories:
    #   boundary_cast_targets: __CG_ arrays with gpu_ sibling → boundary cast at H2D/D2H
    #     (CPU array stays double for serde, GPU gets float)
    #   gpu_transient: transient with gpu_ sibling but NOT __CG_ → change both dtypes
    #     (internal computation, no serde boundary)
    #   pure_transient: no gpu_ sibling → change dtype directly
    #   (skip gpu_ prefixed names — handled as siblings of the above)
    boundary_cast_targets = [
        n
        for n in array_names
        if n in sdfg.arrays and n.startswith("__CG_") and f"gpu_{n}" in sdfg.arrays
    ]
    gpu_transient = [
        n
        for n in array_names
        if n in sdfg.arrays
        and not n.startswith("__CG_")
        and not n.startswith("gpu_")
        and f"gpu_{n}" in sdfg.arrays
    ]
    pure_transient = [
        n
        for n in array_names
        if n in sdfg.arrays
        and not n.startswith("gpu_")
        and f"gpu_{n}" not in sdfg.arrays
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

    permutation = None
    if options["permute_dimensions"]:
        # Transpose the first two dimensions
        permutation = [1, 0]

    if boundary_cast_targets and not options["build_for_integration"]:
        # Standalone: CPU↔GPU boundary cast at H2D/D2H edges
        inject_boundary_cast(
            sdfg,
            array_names=boundary_cast_targets,
            external_dtype=external_dtype,
            permutation=permutation,
        )
    # Integration: GPU→GPU cast is deferred until after integration transforms
    if gpu_transient:
        # Transient arrays with GPU siblings: change both CPU and GPU dtype.
        # No boundary cast needed — internal computation, no serde.
        print(
            f"Lowering {len(gpu_transient)} GPU transient arrays to native {external_dtype}: {gpu_transient}"
        )
        for name in gpu_transient:
            _propagate_dtype(sdfg, name, external_dtype)
            _propagate_dtype(sdfg, f"gpu_{name}", external_dtype)
            if permutation:
                from utils.boundary_cast import _propagate_permutation

                _propagate_permutation(sdfg, f"gpu_{name}", permutation)
    if pure_transient:
        # Pure transient arrays (no GPU sibling): change dtype directly,
        # no cast needed — computation runs natively in lower precision.
        print(
            f"Lowering {len(pure_transient)} pure transient arrays to native {external_dtype}: {pure_transient}"
        )
        for name in pure_transient:
            _propagate_dtype(sdfg, name, external_dtype)
            if permutation:
                from utils.boundary_cast import _propagate_permutation

                _propagate_permutation(sdfg, name, permutation)
    if gpu_only:
        # GPU-only arrays (no CPU counterpart — created by ToGPU pass):
        # change dtype directly.
        print(
            f"Lowering {len(gpu_only)} GPU-only arrays to native {external_dtype}: {gpu_only}"
        )
        for name in gpu_only:
            _propagate_dtype(sdfg, name, external_dtype)
            if permutation:
                from utils.boundary_cast import _propagate_permutation

                _propagate_permutation(sdfg, name, permutation)

    # Lower float64 scalars used in GPU computation.
    # CFL-related scalars stay double (they feed the CFL reduction which is excluded).
    _CFL_SCALARS = {
        "max_vcfl_dyn_var_152",
        "__CG_p_diag__m_max_vcfl_dyn",
        "tmp_call_1",
        "tmp_call_18",
    }
    scalars_to_lower: list[str] = []
    scalars_excluded = sorted(_CFL_SCALARS)
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
                desc = sdfg.arrays[name]
                if desc.transient:
                    # Internal transients can be safely lowered everywhere
                    _propagate_dtype(sdfg, name, external_dtype)
                else:
                    # Interface parameters (non-transient): Preserve top-level double,
                    # lower only inside nested scopes to trigger DaCe's auto-casting.
                    for state in sdfg.states():
                        for node in state.nodes():
                            if isinstance(node, nodes.NestedSDFG):
                                for edge in state.in_edges(node):
                                    if edge.data.data == name and edge.dst_conn:
                                        _propagate_dtype(
                                            node.sdfg, edge.dst_conn, external_dtype
                                        )

    # Lower transient double scalars inside nested SDFGs (GPU kernel
    # intermediates like difcoef, tmp_arg_*, w_con_e, etc.).
    # These are not visible at the top level — _propagate_dtype doesn't
    # reach them. Only maxvcfl must stay double (feeds CFL reduction).
    # Scalars that are comparison results (e.g. _if_cond_*) get int32
    # instead — they're logically boolean, not floating-point.
    _CFL_NESTED_SCALARS = {"maxvcfl", "tmp_call_1"}
    _nested_lowered = 0
    _nested_to_int32 = 0
    if options.get("lower_all"):
        # First pass: find scalars that are comparison outputs
        _comparison_scalars: set[str] = set()
        for nsdfg in sdfg.all_sdfgs_recursive():
            if nsdfg is sdfg:
                continue
            for state in nsdfg.states():
                for node in state.nodes():
                    if not isinstance(node, nodes.Tasklet):
                        continue
                    code = node.code.as_string
                    if not any(
                        op in code
                        for op in (" > ", " < ", " >= ", " <= ", " == ", " != ")
                    ):
                        continue
                    for e in state.out_edges(node):
                        if (
                            isinstance(e.dst, nodes.AccessNode)
                            and e.dst.data in nsdfg.arrays
                            and isinstance(nsdfg.arrays[e.dst.data], dace.data.Scalar)
                        ):
                            _comparison_scalars.add(e.dst.data)

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
                    if name in _comparison_scalars:
                        arr.dtype = dace.int32
                        _nested_to_int32 += 1
                    else:
                        arr.dtype = external_dtype
                        _nested_lowered += 1

    # BFP packing: change GPU arrays to uint8[packed_size], insert CPU-side
    # pack Map, replace H2D edge with packed version.
    if options["lowprec"].startswith("bfp") and BFP_ARRAYS:
        inject_bfp_packing(sdfg, BFP_ARRAYS)

    # Apply transformations
    inverse_strides(sdfg, "gpu_levmask")
    sdfg.validate()

    if options["reduce_bitwidth"]:
        sdfg = downcast_to_minimal_bitwidth(sdfg)

    int64_to_int32(sdfg)

    # Rm copy/memset kernels with API calls
    AssignmentAndCopyKernelToMemsetAndMemcpy().apply_pass(sdfg, {})

    if options["tile"]:
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

    # Sync first
    insert_synchronization_for_profiling(sdfg)
    insert_timers_for_profiling(sdfg)
    sdfg.validate()

    if options["profile"]:
        create_profile_sdfg(sdfg)

    # Floatify: replace double literals and math functions in tasklets and
    # interstate edge conditions to prevent FP64 promotion on GPU.
    # Done last so it sees final tasklet code after tiling/coarsening.
    _FLOATIFY_CONSTANTS = ["0.5", "0.85", "1.0", "0.0", "0.05", "0.65", "1.15"]

    if options["lowprec"] != "fp64":
        # Pass 1: Interstate edge conditions (if-conditions in generated CUDA).
        # These use Python-syntax code blocks; wrap literals with float().
        for nsdfg in sdfg.all_sdfgs_recursive():
            for edge in nsdfg.edges():
                cond = edge.data.condition
                if cond is None:
                    continue
                code = cond.as_string
                if not code or code.strip() in ("1", "true", "True"):
                    continue
                new_code = code
                for val in _FLOATIFY_CONSTANTS:
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
                if new_code != code:
                    edge.data.condition = dace.properties.CodeBlock(new_code)

        # Pass 2: Tasklet code.
        for node, state in sdfg.all_nodes_recursive():
            if not isinstance(node, nodes.Tasklet):
                continue

            code = node.code.as_string
            new_code = code

            # Target constants
            constants = _FLOATIFY_CONSTANTS

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

    # --- Integration transforms (stage 9 equivalent) ---
    # When building for integration into ICON's solve_nonhydrostatic,
    # the generated library must operate in-place on OpenACC-managed GPU
    # memory instead of allocating its own buffers.
    allocation_names_to_comment_out = set()
    if options["build_for_integration"]:
        shallow_copy_used_structs = ["p_prog", "p_int", "p_metrics", "p_patch", "p_diag"]
        deflatten_used_structs = ["p_diag"]
        allocation_names_to_comment_out = change_flatten_lib_to_shallow_copy(
            sdfg, shallow_copy_used_structs, deflatten_used_structs
        )
        sdfg.validate()

        input_to_gpu(sdfg, "z_w_concorr_me")
        input_to_gpu(sdfg, "z_kin_hor_e")
        input_to_gpu(sdfg, "z_vt_ie")
        sdfg.validate()

        _make_flat_gpu_input(sdfg)
        sdfg.validate()

        remove_profiling_states(sdfg)
        remove_sync_states(sdfg)
        insert_program_entry_exit_syncs(sdfg)
        rm_redundant_copies(sdfg)

        # GPU→GPU boundary cast: all data is now GPU-resident at fp64.
        # Cast to lower precision for computation, reverse-cast outputs back.
        if external_dtype != dace.float64:
            # z_* arrays are scratch pads — overwritten by solve_nonhydro after VT.
            # No boundary cast needed (no input to cast in, no output to cast back).
            # Struct fields: gpu___CG_* arrays that survived integration transforms
            _OUTPUT_PREFIXES = ("gpu___CG_p_prog__", "gpu___CG_p_diag__")
            gpu_cast_targets = [
                f"gpu_{n}" for n in boundary_cast_targets
                if f"gpu_{n}" in sdfg.arrays
            ]
            gpu_output_names = [
                n for n in gpu_cast_targets
                if any(n.startswith(p) for p in _OUTPUT_PREFIXES)
            ]
            # Pure outputs: written before read, no need for h2d cast.
            _OUTPUT_ONLY = {
                "gpu___CG_p_diag__m_ddt_vn_apc_pc",
                "gpu___CG_p_diag__m_ddt_w_adv_pc",
            }
            gpu_output_only_names = [
                n for n in gpu_output_names if n in _OUTPUT_ONLY
            ]
            if gpu_cast_targets:
                inject_gpu_boundary_cast(
                    sdfg,
                    gpu_cast_targets,
                    external_dtype,
                    output_names=gpu_output_names,
                    output_only_names=gpu_output_only_names,
                )
            sdfg.validate()

    # --- Collect arrays/scalars NOT lowered and why ---
    all_lowered_arrays = set(boundary_cast_targets) | set(gpu_transient) | set(pure_transient) | set(gpu_only) | set(gpu_sibling) | set(bfp_targets)
    sensitivity_ruled_out_set = set(SENSITIVITY_RULED_OUT)
    sensitivity_borderline_set = set(SENSITIVITY_BORDERLINE)
    sensitivity_candidates_set = set(SENSITIVITY_CANDIDATES)
    _CFL_EXCLUDE_SET = {"maxvcfl"}

    arrays_not_lowered: list[tuple[str, str]] = []
    for name, arr in sdfg.arrays.items():
        if not isinstance(arr, dace.data.Array) or arr.total_size == 1:
            continue
        if arr.dtype != dace.float64:
            continue
        if name in all_lowered_arrays or f"gpu_{name}" in all_lowered_arrays or name.replace("gpu_", "", 1) in all_lowered_arrays:
            continue
        # Determine reason
        bare = name.replace("gpu_", "", 1) if name.startswith("gpu_") else name
        if bare in _CFL_EXCLUDE_SET:
            reason = "CFL exclusion"
        elif bare in sensitivity_ruled_out_set:
            reason = "sensitivity too high"
        elif bare in sensitivity_borderline_set:
            reason = "sensitivity borderline — held for staged testing"
        elif not options.get("lower_all") and bare not in sensitivity_candidates_set:
            reason = "not in sensitivity candidate list"
        else:
            reason = "not a candidate (gpu_ sibling or other)"
        arrays_not_lowered.append((name, reason))

    scalars_not_lowered: list[tuple[str, str]] = []
    for name, arr in sdfg.arrays.items():
        if not isinstance(arr, dace.data.Scalar) or arr.dtype != dace.float64:
            continue
        if name in set(scalars_to_lower):
            continue
        if name in _CFL_SCALARS:
            scalars_not_lowered.append((name, "CFL scalar"))
        else:
            scalars_not_lowered.append((name, "lower_all not set" if not options.get("lower_all") else "unknown"))

    # --- Post-lowering audit: walk ALL SDFGs for remaining fp64 ---
    # Only meaningful when target is lower than fp64; skip for fp64 builds.
    # Classify each remaining fp64 as expected (with reason) or UNEXPECTED.
    _boundary_cast_set = set(boundary_cast_targets)
    _gpu_sibling_set = set(gpu_sibling)
    _cfl_all = _CFL_SCALARS | _CFL_NESTED_SCALARS
    fp64_expected: list[tuple[str, str, str]] = []   # (location, "name (kind)", reason)
    fp64_unexpected: list[tuple[str, str]] = []       # (location, "name (kind)")
    if external_dtype != dace.float64:
        for nsdfg in sdfg.all_sdfgs_recursive():
            is_top = nsdfg is sdfg
            loc = f"{sdfg.name} (top-level)" if is_top else nsdfg.name
            for name, arr in nsdfg.arrays.items():
                if arr.dtype != dace.float64:
                    continue
                kind = "Array" if isinstance(arr, dace.data.Array) else "Scalar"
                entry = f"{name} ({kind})"
                # Classify
                bare = name.replace("gpu_", "", 1) if name.startswith("gpu_") else name
                if is_top and bare in _boundary_cast_set:
                    fp64_expected.append((loc, entry, "CPU-side of boundary-cast"))
                elif is_top and name in _gpu_sibling_set:
                    fp64_expected.append((loc, entry, "GPU sibling of boundary-cast"))
                elif bare in _cfl_all:
                    fp64_expected.append((loc, entry, "CFL exclusion"))
                else:
                    fp64_unexpected.append((loc, entry))

    _write_worklog(
        sdfg_name=sdfg.name,
        options=options,
        external_dtype=external_dtype,
        boundary_cast_targets=boundary_cast_targets,
        gpu_transient=gpu_transient,
        pure_transient=pure_transient,
        gpu_only=gpu_only,
        gpu_sibling=gpu_sibling,
        bfp_targets=bfp_targets,
        scalars_lowered=scalars_to_lower,
        scalars_excluded=scalars_excluded,
        nested_scalars_lowered=_nested_lowered,
        nested_scalars_to_int32=_nested_to_int32,
        nested_scalars_excluded=sorted(_CFL_NESTED_SCALARS | _CFL_SCALARS),
        arrays_not_lowered=arrays_not_lowered,
        scalars_not_lowered=scalars_not_lowered,
        fp64_expected=fp64_expected,
        fp64_unexpected=fp64_unexpected,
    )

    return sdfg, {
        "bfp_targets": bfp_targets,
        "allocation_names_to_comment_out": allocation_names_to_comment_out,
    }


def _compile_kwargs_from_metadata(all_metadata):
    """Extract compile kwargs from optimization metadata."""
    alloc_names = set()
    for m in all_metadata:
        if isinstance(m, dict):
            alloc_names |= m.get("allocation_names_to_comment_out", set())
    kwargs = {}
    if alloc_names:
        kwargs["allocation_names_to_comment_out"] = alloc_names
    return kwargs


def main():
    # Clear worklog from previous runs before appending
    Path(WORKLOG_FILE).unlink(missing_ok=True)
    common.standard_main(
        STAGE_ID,
        optimization_action,
        compile_extra_kwargs=_compile_kwargs_from_metadata,
    )


if __name__ == "__main__":
    main()
