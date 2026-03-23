"""hackintosh.py — SDFG-specific hacks and workarounds.

Run on an already-optimized SDFG (e.g., cloudsc_opt.sdfgz).
"""

from __future__ import annotations
import argparse

import dace
from dace.sdfg.state import LoopRegion


# Loops where zvqx read/write at constant index blocks LoopToMap.
# We replace zvqx access nodes with a fresh scalar to break the overlap.
ZVQX_HACK_LOOPS = {
    "if_764_body_for_765_br0",
    "if_764_body_for_765_br2",
}


def fix_symbol_array_clashes(sdfg: dace.SDFG):
    """Rewrite ISE assignments whose RHS is a bare array name to use array[0].

    The GPU pipeline's symbol propagation replaces `sym = val` by substituting
    `sym` with `val` everywhere. When `val` is an array name, this creates
    symbol/array name clashes that break codegen. Adding `[0]` makes the RHS
    a data access, which the propagation step already skips.
    """
    fixed = 0
    for sd in sdfg.all_sdfgs_recursive():
        arrays = sd.arrays
        for edge in sd.all_interstate_edges():
            if not edge.data.assignments:
                continue
            for k, v in edge.data.assignments.items():
                sv = str(v)
                if sv in arrays:
                    edge.data.assignments[k] = f"{sv}[0]"
                    fixed += 1
    if fixed:
        print(f"  Rewrote {fixed} ISE assignments (bare array name -> array[0])")


def replace_zvqx_with_scalar(sdfg: dace.SDFG):
    """Replace all zvqx references in target loops with a per-loop scalar."""
    patched = 0
    hack_ctr = 0
    scalar_sub = dace.subsets.Range([(0, 0, 1)])

    for node, _ in sdfg.all_nodes_recursive():
        if not isinstance(node, LoopRegion):
            continue
        if node.label not in ZVQX_HACK_LOOPS:
            continue

        hack_ctr += 1
        hack_name = f"zvqx_hack{hack_ctr}"
        if hack_name not in sdfg.arrays:
            sdfg.add_scalar(hack_name, dace.float64, transient=True)

        for st in node.all_states():
            # 1. Redirect data nodes
            for dn in st.data_nodes():
                if dn.data == "zvqx":
                    dn.data = hack_name
                    patched += 1
                    print(f"  Node: {node.label} / {st.label} / zvqx -> {hack_name}")

            # 2. Fix ALL memlets that reference zvqx
            for e in st.edges():
                if e.data.data == "zvqx":
                    e.data.data = hack_name
                    e.data.subset = scalar_sub
                    if e.data.other_subset:
                        e.data.other_subset = scalar_sub

    return patched


def loop_to_map(sdfg: dace.SDFG) -> int:
    """Apply LoopToMap one at a time."""
    from dace.transformation.interstate import LoopToMap
    from tqdm import tqdm

    expr = LoopToMap.expressions()[0]
    pattern_node = list(expr.nodes())[0]

    converted = 0
    skipped = []
    pbar = tqdm(desc="LoopToMap (hack)", unit="loop")

    while True:
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
            break

        if not applied_any:
            break

    pbar.close()
    return converted


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hackintosh SDFG optimizer")
    parser.add_argument("input", type=str, nargs='?', default="cloudsc_opt.sdfgz",
                        help="Input SDFG file (default: cloudsc_opt.sdfgz)")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Output SDFG file (default: <input>_hack.sdfgz)")
    args = parser.parse_args()

    print(f"Reading: {args.input}")
    sdfg = dace.SDFG.from_file(args.input)

    # 1. Replace zvqx with per-loop scalars to unblock LoopToMap
    n = replace_zvqx_with_scalar(sdfg)
    print(f"zvqx replacements: {n}")
    sdfg.validate()

    # 2. Run LoopToMap to pick up the newly-unblocked loops
    n_l2m = loop_to_map(sdfg)
    print(f"LoopToMap (hack): converted {n_l2m} loops")

    # 3. Remove symbols that clash with array names (breaks codegen)
    fix_symbol_array_clashes(sdfg)

    # 4. Simplify
    sdfg.simplify()

    out_path = args.output or args.input.replace(".sdfgz", "_hack.sdfgz")
    print(f"Writing: {out_path}")
    sdfg.save(out_path, compress=True)
    sdfg.validate()
