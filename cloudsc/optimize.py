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
from dace.transformation.passes.analysis import loop_analysis

from ssa import (ssa_transform, ssa_transform_wtr, isolate_loop_variables, privatize_scalars,
                 expand_scalars, propagate_constants, unroll_loops)

SYMBOL_MAP = {
    "nclv": 5,
    "ncldql": 1,
    "ncldqi": 2,
    "ncldqr": 3,
    "ncldqs": 4,
    "ncldqv": 5,
}


def fix_missing_nsdfg_symbols(sdfg: dace.SDFG):
    """Propagate missing symbols into NestedSDFGs."""
    fixed = 0
    for node, _ in sdfg.all_nodes_recursive():
        if isinstance(node, nd.NestedSDFG):
            nsdfg = node.sdfg
            connectors = set(node.in_connectors.keys()) | set(node.out_connectors.keys())
            for sym in nsdfg.free_symbols:
                if sym not in connectors and sym not in node.symbol_mapping:
                    node.symbol_mapping[sym] = dace.symbolic.pystr_to_symbolic(sym)
                    fixed += 1
    if fixed:
        print(f"  Fixed {fixed} missing NestedSDFG symbol mappings")


def checkpoint(sdfg: dace.SDFG, name: str, out_dir: str):
    """Validate and save a checkpoint SDFG."""
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
    parser.add_argument("--start-from", type=str, default=None,
                        help="Start from a checkpoint (e.g. after_l2m)")
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.input)
    only_ssa = set(args.only_ssa.split(",")) if args.only_ssa else None
    out_dir = os.path.dirname(args.output or args.input)

    # Jump to a checkpoint if --start-from is given
    start_from = args.start_from
    skip_steps = set()
    if start_from:
        step_order = ["propagate", "ssa", "isolate", "privatize", "expand", "l2m"]
        for step in step_order:
            skip_steps.add(step)
            if step == start_from.replace("after_", ""):
                break
        ckpt = os.path.join(out_dir, f"{start_from}.sdfgz")
        if os.path.exists(ckpt):
            print(f"Loading checkpoint: {ckpt}")
            sdfg = dace.SDFG.from_file(ckpt)

    # 1. Constant propagation — nclv and related indices become concrete
    if not args.no_propagate and "propagate" not in skip_steps:
        propagate_constants(sdfg, SYMBOL_MAP)
        fix_missing_nsdfg_symbols(sdfg)
        checkpoint(sdfg, "after_propagate", out_dir)

    # 2. SSA — split multi-write scalars into unique versions
    if not args.no_ssa and "ssa" not in skip_steps:
        # ssa_transform_wtr(sdfg)
        ssa_result = ssa_transform(sdfg, only=only_ssa)
        print(f"SSA: {sum(len(v) for v in ssa_result.values())} versions "
              f"for {len(ssa_result)} variables")
        checkpoint(sdfg, "after_ssa", out_dir)

    # 3. Loop variable isolation — unique itervar per LoopRegion
    if not args.no_isolate and "isolate" not in skip_steps:
        isolate_loop_variables(sdfg)
        checkpoint(sdfg, "after_isolate", out_dir)

    # 4. Scalar privatization — fresh transients for loop-private scalars
    if not args.no_privatize and "privatize" not in skip_steps:
        privatize_scalars(sdfg)
        checkpoint(sdfg, "after_privatize", out_dir)

    # 5. Scalar expansion — promote blocking scalars to arrays
    if not args.no_expand and "expand" not in skip_steps:
        expand_scalars(sdfg)
        checkpoint(sdfg, "after_expand", out_dir)

    # 6. LoopToMap — convert eligible control-flow loops into dataflow maps
    if not args.no_l2m and "l2m" not in skip_steps:
        n = sdfg.apply_transformations_repeated(LoopToMap, validate=False)
        print(f"LoopToMap: converted {n} loops to maps")
        checkpoint(sdfg, "after_l2m", out_dir)

    # 7. Unroll — unroll maps/loops with nclv-sized extents
    if not args.no_unroll:
        unroll_loops(sdfg, SYMBOL_MAP)
        checkpoint(sdfg, "after_unroll", out_dir)

    # # 2. SSA — split multi-write scalars into unique versions
    # ssa_result = ssa_transform(sdfg, only=only_ssa)
    # print(f"SSA: {sum(len(v) for v in ssa_result.values())} versions "
    #         f"for {len(ssa_result)} variables")
    # checkpoint(sdfg, "after_ssa_p2", out_dir)

    # # 3. Loop variable isolation — unique itervar per LoopRegion
    # isolate_loop_variables(sdfg)
    # checkpoint(sdfg, "after_isolate_p2", out_dir)

    # # 4. Scalar privatization — fresh transients for loop-private scalars
    # privatize_scalars(sdfg)
    # checkpoint(sdfg, "after_privatize_p2", out_dir)

    # # 5. Scalar expansion — promote blocking scalars to arrays
    # expand_scalars(sdfg)
    # checkpoint(sdfg, "after_expand_p2", out_dir)

    # # 6. LoopToMap — convert eligible control-flow loops into dataflow maps
    # n = sdfg.apply_transformations_repeated(LoopToMap, validate=False)
    # print(f"LoopToMap: converted {n} loops to maps")
    # checkpoint(sdfg, "after_l2m_p2", out_dir)

    # for node, _ in sdfg.all_nodes_recursive():
    #     if isinstance(node, dace.sdfg.state.LoopRegion):
    #         start = loop_analysis.get_init_assignment(node)
    #         end = loop_analysis.get_loop_end(node)
    #         if str(start) == 'kidia':
    #             raise AssertionError(f"Horizontal LoopRegion {node.label} ({start} to {end}) "
    #                                     "was not converted to a Map. This usually means "
    #                                     "there is a data dependency blocking LoopToMap.")

    # 8. Fix missing NestedSDFG symbols, then simplify
    fix_missing_nsdfg_symbols(sdfg)
    sdfg.simplify()
    checkpoint(sdfg, "after_simplify", out_dir)

    out_path = args.output or args.input.replace(".sdfgz", "_opt.sdfgz")
    sdfg.save(out_path, compress=True)
    print(f"Saved to {out_path}")
