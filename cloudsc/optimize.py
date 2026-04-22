"""optimize.py — Run SSA + LoopToMap on a DaCe SDFG.

Usage:
    python optimize.py cloudsc_pydace_simplified_symbolic.sdfgz -o cloudsc_opt.sdfgz
"""

from __future__ import annotations
import argparse
import os

import dace
from dace import nodes as nd
from dace.transformation.interstate import LoopToMap


from ssa import (ssa_transform, ssa_transform_wtr, isolate_loop_variables, privatize_scalars,
                 expand_scalars, propagate_constants, unroll_loops)
from ssa.condition_fusion import fuse_all_conditions, hoist_invariant_conditions
from ssa.graph_utils import lift_data_refs_in_conditions, scalarize_small_arrays

SYMBOL_MAP = {
    "nclv": 5,
    "ncldql": 1,
    "ncldqi": 2,
    "ncldqr": 3,
    "ncldqs": 4,
    "ncldqv": 5,
}


def loop_to_map(sdfg: dace.SDFG) -> int:
    """Apply LoopToMap directly to each LoopRegion, one at a time."""
    from dace.sdfg.state import LoopRegion
    from tqdm import tqdm

    expr = LoopToMap.expressions()[0]
    pattern_node = list(expr.nodes())[0]

    converted = 0
    skipped = []
    pbar = tqdm(desc="LoopToMap", unit="loop")

    while True:
        # Re-collect after each conversion (graph references go stale)
        sdfg.reset_cfg_list()
        candidates = []
        for sd in sdfg.all_sdfgs_recursive():
            for cfg in sd.all_control_flow_regions():
                for child in cfg.nodes():
                    if isinstance(child, LoopRegion):
                        candidates.append((child, cfg, sd))

        if not candidates:
            break

        pbar.total = (pbar.n or 0) + len(candidates)
        pbar.refresh()

        applied_any = False
        for loop, parent_cfg, sub_sdfg in candidates:
            if loop.label in skipped:
                continue
            pbar.set_postfix_str(loop.label)

            xform = LoopToMap()
            try:
                node_id = parent_cfg.node_id(loop)
                xform.setup_match(sub_sdfg, parent_cfg.cfg_id, -1,
                                  {pattern_node: node_id}, 0)
                if not xform.can_be_applied(parent_cfg, 0, sub_sdfg):
                    skipped.append(loop.label)
                    continue
                xform.apply(parent_cfg, sub_sdfg)
            except Exception as e:
                pbar.write(f"  SKIP {loop.label}: {e}")
                skipped.append(loop.label)
                continue

            converted += 1
            applied_any = True
            pbar.update(1)
            break  # Re-collect

        if not applied_any:
            break

    pbar.close()
    return converted


def condition_fusion(sdfg: dace.SDFG) -> int:
    """Fuse ConditionalBlocks inside LoopRegions into single giant CBs."""
    return fuse_all_conditions(sdfg)


def checkpoint(sdfg: dace.SDFG, name: str, out_dir: str):
    """Validate and save a checkpoint SDFG.

    Also refresh the cfg_list so downstream passes (and any pattern matching
    they invoke) don't see stale CFG indices. Cheap insurance against the
    DaCe bug where `cfg_list[cfg_id]` returns the wrong region after mutations.
    """
    sdfg.reset_cfg_list()
    path = os.path.join(out_dir, f"{name}.sdfgz")
    sdfg.save(path, compress=True)
    print(f"  checkpoint: {path}")
    sdfg.validate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SSA + LoopToMap optimizer")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Output SDFG file (default: <input>_opt.sdfgz)")
    parser.add_argument("--only-ssa", type=str, default=None,
                        help="Comma-separated list of variable names to SSA (debug)")
    parser.add_argument("--no-ssa", action="store_true",
                        help="Skip SSA pass")
    parser.add_argument("--no-isolate", action="store_true",
                        help="Skip loop variable isolation")
    parser.add_argument("--no-privatize", action="store_true",
                        help="Skip scalar privatization")
    parser.add_argument("--no-expand", action="store_true",
                        help="Skip scalar expansion")
    parser.add_argument("--no-l2m", action="store_true",
                        help="Skip LoopToMap")
    parser.add_argument("--no-unroll", action="store_true",
                        help="Skip unroll")
    parser.add_argument("--no-propagate", action="store_true",
                        help="Skip constant propagation")
    parser.add_argument("--no-liftcond", action="store_true",
                        help="Skip loop-conditional data-ref lifting")
    parser.add_argument("--no-condfuse", action="store_true",
                        help="Skip condition fusion/hoist")
    parser.add_argument("--no-arrpriv", action="store_true",
                        help="Skip array privatization")
    parser.add_argument("--no-arrexp", action="store_true",
                        help="Skip array expansion")
    parser.add_argument("--no-mapfuse", action="store_true",
                        help="Skip MapFusionVertical")
    parser.add_argument("--start-from", type=str, default=None,
                        help="Start from a checkpoint (e.g. after_l2m)")
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.input)
    only_ssa = set(args.only_ssa.split(",")) if args.only_ssa else None
    # Default output and checkpoint dir: build/sdfgz/
    if not args.output:
        stem = os.path.splitext(os.path.basename(args.input))[0]
        args.output = os.path.join("build", "sdfgz", f"{stem}_opt.sdfgz")
    out_dir = os.path.dirname(args.output) or "build/sdfgz"
    os.makedirs(out_dir, exist_ok=True)

    # Jump to a checkpoint if --start-from is given
    start_from = args.start_from
    skip_steps = set()
    if start_from:
        step_order = ["liftcond", "propagate", "unroll", "simplify1", "ssa",
                      "isolate", "privatize", "expand", "arrexp", "l2m",
                      "condfuse", "condhoist", "privatize2", "l2m2", "arrpriv",
                      "mapfusion", "simplify"]
        for step in step_order:
            skip_steps.add(step)
            if step == start_from.replace("after_", ""):
                break
        ckpt = os.path.join(out_dir, f"{start_from}.sdfgz")
        if os.path.exists(ckpt):
            print(f"Loading checkpoint: {ckpt}")
            sdfg = dace.SDFG.from_file(ckpt)

    # 1. Lift data refs in conditional metadata to symbols
    if not args.no_liftcond and "liftcond" not in skip_steps:
        lift_data_refs_in_conditions(sdfg)
        checkpoint(sdfg, "after_liftcond", out_dir)

    # 2. Constant propagation — nclv and related indices become concrete
    if not args.no_propagate and "propagate" not in skip_steps:
        propagate_constants(sdfg, SYMBOL_MAP)
        checkpoint(sdfg, "after_propagate", out_dir)

    # 3. Unroll — unroll maps/loops with nclv-sized extents
    if not args.no_unroll and "unroll" not in skip_steps:
        unroll_loops(sdfg)
        checkpoint(sdfg, "after_unroll", out_dir)

    # 3b. Simplify after unroll (state fusion, dead code, etc.)
    if "simplify1" not in skip_steps:
        sdfg.simplify()
        checkpoint(sdfg, "after_simplify1", out_dir)

    # 4. SSA — split multi-write scalars into unique versions
    if not args.no_ssa and "ssa" not in skip_steps:
        ssa_result = ssa_transform(sdfg, only=only_ssa)
        print(f"SSA: {sum(len(v) for v in ssa_result.values())} versions "
              f"for {len(ssa_result)} variables")
        checkpoint(sdfg, "after_ssa", out_dir)

    # 5. Loop variable isolation — unique itervar per LoopRegion
    if not args.no_isolate and "isolate" not in skip_steps:
        isolate_loop_variables(sdfg)
        checkpoint(sdfg, "after_isolate", out_dir)

    # 6. Scalar privatization — fresh transients for loop-private scalars
    if not args.no_privatize and "privatize" not in skip_steps:
        privatize_scalars(sdfg)
        checkpoint(sdfg, "after_privatize", out_dir)

    # 7. Scalar expansion — promote blocking scalars to arrays
    if not args.no_expand and "expand" not in skip_steps:
        expand_scalars(sdfg, diagnose=True)
        checkpoint(sdfg, "after_expand", out_dir)

    # 7b. Array expansion — widen small transients with a loop-itervar dim so
    #     per-iteration "scratch" array writes stop looking like races.
    if not args.no_arrexp and "arrexp" not in skip_steps:
        from ssa.array_expansion import expand_arrays
        expand_arrays(sdfg)
        checkpoint(sdfg, "after_arrexp", out_dir)

    # 8. LoopToMap — convert eligible control-flow loops into dataflow maps
    if not args.no_l2m and "l2m" not in skip_steps:
        n = loop_to_map(sdfg)
        n += loop_to_map(sdfg)
        print(f"LoopToMap: converted {n} loops to maps")
        checkpoint(sdfg, "after_l2m", out_dir)

    # 9. Condition fusion — merge consecutive/nested ConditionalBlocks
    if not args.no_condfuse and "condfuse" not in skip_steps:
        n_fused = condition_fusion(sdfg)
        if n_fused:
            print(f"ConditionFusion: fused {n_fused} conditional blocks")
        checkpoint(sdfg, "after_condfuse", out_dir)

    # 9a. Condition hoist — move loop-invariant conditions above loops
    if not args.no_condfuse and "condhoist" not in skip_steps:
        n_hoisted = hoist_invariant_conditions(sdfg)
        checkpoint(sdfg, "after_condhoist", out_dir)

    # 9b. Isolate + privatize on cloned loops from hoist
    # NOTE: skip SSA round 2 — it breaks memlet refs inside nested SDFGs
    if not args.no_condfuse and "privatize2" not in skip_steps:
        if not args.no_isolate:
            isolate_loop_variables(sdfg)
        if not args.no_privatize:
            privatize_scalars(sdfg)
        checkpoint(sdfg, "after_privatize2", out_dir)

    # 9c. Another round of LoopToMap after condition fusion/hoist
    if not args.no_l2m and "l2m2" not in skip_steps:
        n2 = loop_to_map(sdfg)
        if n2:
            print(f"LoopToMap (post-condfuse): converted {n2} more loops to maps")
        checkpoint(sdfg, "after_l2m2", out_dir)

    # 9d. Array privatization — small transient arrays with external init used
    #     as per-iteration scratch inside the remaining loops.  Correctness-
    #     preserving rename + init clone; doesn't widen shapes.
    if not args.no_arrpriv and "arrpriv" not in skip_steps:
        from ssa.array_privatization import privatize_arrays
        privatize_arrays(sdfg)
        checkpoint(sdfg, "after_arrpriv", out_dir)

    # 9e. MapFusion — merge adjacent maps to cut per-iteration kernel-launch
    #     count. Relies on dace-cloudsc's DMR-consolidation, inout-split, and
    #     itervar-independent-producer rejection fixes (see 51103da8b,
    #     9f7c8397d, 0246bd1d8 on pratyai/support-half).
    if not args.no_mapfuse and "mapfusion" not in skip_steps:
        from dace.transformation.dataflow import MapFusionVertical
        # MapFusionVertical needs adjacent maps in the same state.  The full
        # pipeline leaves each map in its own state (post-LoopToMap), so
        # simplify first to collapse state boundaries via StateFusion.
        sdfg.simplify()
        checkpoint(sdfg, "before_mapfusion_simplify", out_dir)
        before_maps = sum(1 for sd in sdfg.all_sdfgs_recursive()
                          for st in sd.all_states()
                          for n in st.nodes()
                          if isinstance(n, nd.MapEntry))
        sdfg.reset_cfg_list()
        n_mf = 0
        while True:
            k = sdfg.apply_transformations_repeated(
                MapFusionVertical, permissive=False, validate=False)
            if not k:
                break
            n_mf += k
            sdfg.reset_cfg_list()
        after_maps = sum(1 for sd in sdfg.all_sdfgs_recursive()
                         for st in sd.all_states()
                         for n in st.nodes()
                         if isinstance(n, nd.MapEntry))
        print(f"MapFusion: {n_mf} fusions applied ({before_maps} -> {after_maps} maps)")
        checkpoint(sdfg, "after_mapfusion", out_dir)

    # 10. Simplify
    if "simplify" not in skip_steps:
        sdfg.simplify()
        checkpoint(sdfg, "after_simplify", out_dir)

    sdfg.save(args.output, compress=True)
    print(f"Saved to {args.output}")
