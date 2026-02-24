import argparse
import os
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

    if options.get("lower_all"):
        # CFL scalar excluded — feeds the final CFL check which stays double.
        # CFL arrays (gpu_maxvcfl_arr, gpu_vcflmax, vcflmax) are now lowered
        # since reductions are templated and boundary cast handles the rest.
        _CFL_EXCLUDE = {
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
        ]
        print(
            f"Lowering all {len(array_names)} float64 arrays to {external_dtype} for pointwise decompression:\n{array_names}"
        )
    else:
        # Lower only sensitivity-validated safe fields
        array_names = list(SENSITIVITY_CANDIDATES)

    # Exclude BFP arrays from pointwise decompression — they get their own
    # SDFG-level transformation via inject_bfp_packing.
    bfp_set = set(BFP_ARRAYS) if options["lowprec"].startswith("bfp") else set()
    array_names = [n for n in array_names if n not in bfp_set]

    # Split arrays into four categories:
    #   boundary_cast_targets: arrays with gpu_ sibling whose CPU side holds
    #     externally-provided double data → boundary cast at H2D/D2H.
    #     This includes __CG_ arrays (flatten copies from struct fields) and
    #     non-transient arrays (function parameters from Fortran).
    #   gpu_transient: transient with gpu_ sibling, NOT __CG_ and NOT function
    #     param → change both dtypes (internal computation, no external boundary)
    #   pure_transient: no gpu_ sibling → change dtype directly
    #   gpu_only: gpu_ prefixed with no bare counterpart → change dtype directly
    boundary_cast_targets = [
        n
        for n in array_names
        if n in sdfg.arrays
        and not n.startswith("gpu_")
        and (n.startswith("__CG_") or not sdfg.arrays[n].transient)
        and f"gpu_{n}" in sdfg.arrays
    ]
    gpu_transient = [
        n
        for n in array_names
        if n in sdfg.arrays
        and not n.startswith("__CG_")
        and not n.startswith("gpu_")
        and sdfg.arrays[n].transient
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

    if boundary_cast_targets:
        inject_boundary_cast(
            sdfg,
            array_names=boundary_cast_targets,
            external_dtype=external_dtype,
        )
    if gpu_transient:
        # Transient arrays with GPU siblings: change both CPU and GPU dtype.
        # No boundary cast needed — internal computation, no serde.
        print(
            f"Lowering {len(gpu_transient)} GPU transient arrays to native {external_dtype}: {gpu_transient}"
        )
        for name in gpu_transient:
            _propagate_dtype(sdfg, name, external_dtype)
            _propagate_dtype(sdfg, f"gpu_{name}", external_dtype)
    if pure_transient:
        # Pure transient arrays (no GPU sibling): change dtype directly,
        # no cast needed — computation runs natively in lower precision.
        print(
            f"Lowering {len(pure_transient)} pure transient arrays to native {external_dtype}: {pure_transient}"
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
    # Scalars that are comparison results (e.g. _if_cond_*) get int32
    # instead — they're logically boolean, not floating-point.
    _CFL_NESTED_SCALARS = {"maxvcfl", "tmp_call_1"}
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
                    else:
                        arr.dtype = external_dtype

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

    # Floatify: replace double literals and math functions in tasklets with
    # float equivalents to prevent FP64 promotion on GPU.
    # Done last so it sees final tasklet code after tiling/coarsening.
    if options["lowprec"] != "fp64":
        for node, state in sdfg.all_nodes_recursive():
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

    coarsen_coalescable_dim(sdfg, factor=2)

    return sdfg


def main():
    common.standard_main(STAGE_ID, optimization_action)


if __name__ == "__main__":
    main()
