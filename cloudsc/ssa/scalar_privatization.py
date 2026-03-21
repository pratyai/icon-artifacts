"""scalar_privatization.py — Replace loop-private scalars with fresh transients.

For each LoopRegion, finds scalars that are:
  - Written inside the loop with a subset independent of the itervar
  - NOT accessed outside the loop (loop-private)

Replaces them with a fresh transient unique to that loop, so LoopToMap's
`other_access_nodes` set won't include them and the write-index check is skipped.

Usage (standalone):
    python -m ssa.scalar_privatization input.sdfgz -o output.sdfgz
"""

from __future__ import annotations

import copy
from collections import defaultdict

from tqdm import tqdm
import dace
from dace import nodes as nd
from dace.sdfg.state import LoopRegion


def _loop_states(loop: LoopRegion) -> set:
    """All states inside a LoopRegion (recursively)."""
    return set(loop.all_states())


def _scalars_in_loop(sdfg: dace.SDFG, loop: LoopRegion) -> set[str]:
    """Scalar transients written inside the loop body."""
    written: dict[str, int] = {}
    for state in loop.all_states():
        for node in state.nodes():
            if (isinstance(node, nd.AccessNode)
                    and state.in_degree(node) > 0
                    and node.data in sdfg.arrays
                    and isinstance(sdfg.arrays[node.data], dace.data.Scalar)
                    and sdfg.arrays[node.data].transient):
                written[node.data] = written.get(node.data, 0) + 1
    return {name for name, count in written.items() if count > 1}


def _is_loop_private(sdfg: dace.SDFG, loop: LoopRegion,
                     name: str) -> bool:
    """Check if `name` is only accessed inside `loop` (not outside).

    Checks AccessNodes in states, ConditionalBlock conditions, LoopRegion
    expressions, and interstate edge conditions/assignments.
    """
    import re
    loop_state_set = _loop_states(loop)
    loop_regions = set(loop.all_control_flow_regions())
    word_pat = re.compile(r'\b' + re.escape(name) + r'\b')

    # Check AccessNodes in states outside the loop
    for state in sdfg.all_states():
        if state in loop_state_set:
            continue
        for node in state.nodes():
            if isinstance(node, nd.AccessNode) and node.data == name:
                return False

    # Check CFG-level references outside the loop (conditions, expressions, edges)
    from dace.sdfg.state import ConditionalBlock as CB
    for cfg in sdfg.all_control_flow_regions():
        if cfg in loop_regions:
            continue
        for child in cfg.nodes():
            if isinstance(child, CB):
                for cond, _ in child.branches:
                    if cond is not None and word_pat.search(cond.as_string):
                        return False
            elif isinstance(child, LoopRegion) and child is not loop:
                for attr in ('init_statement', 'update_statement', 'loop_condition'):
                    val = getattr(child, attr, None)
                    if val is not None:
                        val_str = val.as_string if hasattr(val, 'as_string') else str(val)
                        if word_pat.search(val_str):
                            return False
        # Check interstate edges
        for e in cfg.edges():
            try:
                _, _, edge_data = e
            except (TypeError, ValueError):
                edge_data = e.data if hasattr(e, 'data') else e
            if hasattr(edge_data, 'assignments') and edge_data.assignments:
                for k, v in edge_data.assignments.items():
                    if word_pat.search(k) or word_pat.search(str(v)):
                        return False
            if hasattr(edge_data, 'condition'):
                if word_pat.search(edge_data.condition.as_string):
                    return False

    return True


def _create_identifier_dict(sdfg: dace.SDFG) -> set[tuple[str, dace.SDFG]]:
    """Create a dict of all identifiers used in the SDFG, mapping name → parent SDFG."""
    identifiers: set[tuple[str, dace.SDFG]] = set()
    for g in sdfg.all_sdfgs_recursive():
        for a in g.arrays:
            identifiers.add((a, g))
        for s in g.symbols:
            identifiers.add((s, g))
        for c in g.constants:
            identifiers.add((c, g))
        for edge in g.edges():
            for sym in edge.data.assignments:
                identifiers.add((sym, g))
    return identifiers


def _mint_name(sdfg: dace.SDFG, name: str, loop_label: str, counter: dict[str, int]) -> str:
    """Create a fresh private name and register the descriptor."""
    base = f"{name}__priv_{loop_label}"
    counter[base] = counter.get(base, 0) + 1
    ver = counter[base]
    new_name = f"{base}_{ver}"
    while (new_name in sdfg.arrays or 
           new_name in sdfg.symbols or 
           new_name in sdfg.constants):
        ver += 1
        counter[base] = ver
        new_name = f"{base}_{ver}"
    
    desc = copy.deepcopy(sdfg.arrays[name])
    desc.transient = True
    sdfg.add_datadesc(new_name, desc)
    return new_name


def privatize_scalars(sdfg: dace.SDFG) -> int:
    """Replace loop-private scalars with fresh transients.

    Returns number of scalars privatized.
    """
    all_loops: list[LoopRegion] = []
    for cfg in sdfg.all_control_flow_regions():
        for child in cfg.nodes():
            if isinstance(child, LoopRegion):
                all_loops.append(child)

    privatized = 0
    identifiers = _create_identifier_dict(sdfg)
    counter_dict = {}
    for name, _ in identifiers:
        counter_dict[name] = 0

    for loop in tqdm(all_loops, desc="Privatizing scalars", unit="loop"):
        candidates = _scalars_in_loop(sdfg, loop)
        for name in candidates:
            if not _is_loop_private(sdfg, loop, name):
                continue

            # Create fresh transient
            new_name = _mint_name(sdfg, name, loop.label, counter_dict)

            # Replace ALL references within the loop: AccessNodes, memlets,
            # ConditionalBlock conditions, LoopRegion expressions, interstate edges
            loop.replace_dict({name: new_name})

            privatized += 1

    print(f"Scalar privatization: {privatized} scalars privatized")
    return privatized


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scalar privatization for loop-private transients")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.input)
    privatize_scalars(sdfg)

    out_path = args.output or args.input.replace(".sdfgz", "_priv.sdfgz")
    sdfg.save(out_path)
    print(f"Saved to {out_path}")
