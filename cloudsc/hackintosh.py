"""hackintosh.py — SDFG-specific hacks and name-matching workarounds.

This script focuses ONLY on the hacks (like forced scalar expansion) 
and should be run on an SDFG that has already been pre-processed 
(e.g., after_privatize.sdfgz).
"""

from __future__ import annotations
import argparse
import os

import dace
from dace.transformation.interstate import LoopToMap
from ssa import expand_scalars

# Set of data arrays (scalars) to force expansion on.
FORCED_EXPANSION = {
    "zdtforc__priv_for_428_1",
    "zdtdp__priv_for_428_1",
    "zqe__priv_for_428_1",
    "zevap__priv_for_428_1",
    "zfallcorr__v1__priv_for_428_1",
    "zzrh__priv_for_428_1",
    "zdqs_index",
    "zevaplimmix_index",
    "zlcust_index_0",
    "zlcust_index_1",
    "zlcust_index_2",
    "zlcust_index_3",
    "zdtgdp_index_3",
    "zdtgdp_index_4",
    "zlcust_slice_minus_zdqs_slice",
    "zlcust_slice_minus_zlfinal",
    "zlfinal_0",
    "zlfinal__v3",
    "zlfinal__v4",
    "zlfinalsum_slice_plus_zlfinal",
    "zlfinalsum_index",
    "zsolqa_index_10",
    "zsolqa_index_8",
    "zsolqa_index_9",
    "zsolqa_slice_minus_zevap",
    "zsolqa_slice_plus_zevap",
    "zfall",
    "zfallsink_slice",
    "zfallsrce_index",
    "zpfplsx_index",
    "zqpretot_index",
    "zfallsrce_slice",
    "zfallsrce_index_0",
    "zqpretot_slice_plus_zqxfg_slice",
    "zsolqa_slice_plus_zlcust_slice",
    "zqxfg_index_17",
    "zsolqa_index_31",
    "zqxfg_index_18",
    "zsolqa_slice_plus_zfallsrce_slice",
    "zrho_index_8",
    "zqxfg_slice_plus_zfallsrce_slice",
    "zvqx_index_0",
}

# Set of loop labels to force convert to Maps, even if safety checks fail.
FORCED_L2M = {
    "for_555",
    "for_597",
    "for_664",
    "if_854_body_for_855",
    "for_561",
    "for_765",
    "if_764_body_for_765",
    "if_910_else_929_if_929_body_for_930",
    "if_953_body_for_954",
    "for_1128",
}

# Set of data arrays to force SSA on.
FORCED_SSA = {
    "llo1__priv_for_428_1",
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hackintosh SDFG optimizer (Hacks only)")
    parser.add_argument("input", type=str, nargs='?', default="cloudsc_opt.sdfgz",
                        help="Input SDFG file (default: cloudsc_opt.sdfgz)")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Output SDFG file (default: <input>_hack.sdfgz)")
    args = parser.parse_args()

    print(f"Reading: {args.input}")
    sdfg = dace.SDFG.from_file(args.input)
    out_dir = os.path.dirname(args.output or args.input)

    # 1. Forced Scalar expansion
    print(f"Running forced scalar expansion on: {FORCED_EXPANSION}")
    expanded = expand_scalars(sdfg, force=FORCED_EXPANSION)
    print(f"  Expanded {expanded} scalars")
    sdfg.validate()

    # 2. Forced SSA
    if FORCED_SSA:
        print(f"Running forced SSA on: {FORCED_SSA}")
        from ssa.data_ssa import ssa_transform
        ssa_transform(sdfg, only=FORCED_SSA)
        sdfg.validate()

    # 3. Forced LoopToMap for specific labels that regular pass might skip
    forced_count = 0
    for node, graph in sdfg.all_nodes_recursive():
        if not isinstance(node, dace.sdfg.state.LoopRegion):
            continue
        if node.label not in FORCED_L2M:
            continue
        print(f"Forcing LoopToMap on {node.label}")
        xform = LoopToMap()
        xform.loop = node
        # We call apply directly to bypass can_be_applied checks
        xform.apply(graph, sdfg)
        forced_count += 1
    
    if forced_count:
        print(f"Forced LoopToMap: converted {forced_count} additional loops")
        sdfg.validate()

    # 3. Simplify
    sdfg.simplify()

    if args.output:
        out_path = args.output
    else:
        out_path = args.input.replace(".sdfgz", "_hack.sdfgz")
    
    print(f"Writing: {out_path}")
    sdfg.save(out_path, compress=True)
    sdfg.validate()
