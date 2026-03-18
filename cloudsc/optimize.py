"""optimize.py — Run SSA + LoopToMap on a DaCe SDFG.

Usage:
    python optimize.py cloudsc_pydace_simplified_symbolic.sdfgz -o cloudsc_opt.sdfgz
"""

from __future__ import annotations
import argparse

import dace
from dace.transformation.interstate import LoopToMap

from ssa import ssa_transform, isolate_loop_variables


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
    parser.add_argument("--no-l2m", action="store_true",
                        help="Skip LoopToMap")
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.input)
    only_ssa = set(args.only_ssa.split(",")) if args.only_ssa else None

    # 1. SSA — split multi-write scalars into unique versions
    if not args.no_ssa:
        ssa_result = ssa_transform(sdfg, only=only_ssa)
        print(f"SSA: {sum(len(v) for v in ssa_result.values())} versions "
              f"for {len(ssa_result)} variables")

    # 2. Loop variable isolation — unique itervar per LoopRegion
    if not args.no_isolate:
        isolate_loop_variables(sdfg)

    # 3. LoopToMap — convert eligible control-flow loops into dataflow maps
    if not args.no_l2m:
        n = sdfg.apply_transformations_repeated(LoopToMap, validate=False)
        print(f"LoopToMap: converted {n} loops to maps")

    out_path = args.output or args.input.replace(".sdfgz", "_opt.sdfgz")
    sdfg.save(out_path)
    print(f"Saved to {out_path}")
