"""scalar_expansion.py — Expand scalars to arrays indexed by inner-loop itervar.

When a scalar blocks LoopToMap because its write index doesn't depend on the
inner loop's itervar, we promote it to a 1-D array indexed by that itervar.
This lets each iteration write to its own slot, satisfying LoopToMap's
write-index check.

Safety constraint: a scalar is only expanded if ALL its accesses within the
parent loop are inside inner loops with compatible ranges.  Accesses "loose"
in the parent body (outside any inner loop) would not have an itervar in
scope to index with, so those scalars are skipped.

Usage (standalone):
    python -m ssa.scalar_expansion input.sdfgz -o output.sdfgz
"""

from __future__ import annotations

import sympy as sp

import dace
from dace import nodes as nd, symbolic, subsets as sbs
from dace.sdfg.state import LoopRegion
from dace.transformation.passes.analysis import loop_analysis
from dace.transformation.interstate.loop_to_map import _check_range


def _get_loop_range(loop: LoopRegion):
    """Return (itervar, start, end, step) or None if not determinable."""
    start = loop_analysis.get_init_assignment(loop)
    end = loop_analysis.get_loop_end(loop)
    step = loop_analysis.get_loop_stride(loop)
    itervar = loop.loop_variable
    if any(x is None for x in (start, end, step, itervar)):
        return None
    return itervar, start, end, step


def _blocked_scalars(sdfg: dace.SDFG, loop: LoopRegion) -> set[str]:
    """Return set of scalar names that block this loop from LoopToMap.

    A scalar blocks if it's in `other_access_nodes`, is written in the loop,
    and the write index doesn't depend on the itervar.
    """
    info = _get_loop_range(loop)
    if info is None:
        return set()
    itervar, start, end, step = info

    _, write_set = loop.read_and_write_sets()
    loop_states = set(loop.all_states())

    # Replicate LoopToMap's other_access_nodes computation
    other_access_nodes = set()
    for state in sdfg.states():
        if state in loop_states:
            continue
        other_access_nodes |= {
            n.data for n in state.data_nodes()
            if sdfg.arrays[n.data].transient
        }
    for state in loop_states:
        other_access_nodes |= {
            n.data for n in state.data_nodes()
            if not sdfg.arrays[n.data].transient
        }

    itersym = symbolic.pystr_to_symbolic(itervar)
    a = sp.Wild('a', exclude=[itersym])
    b = sp.Wild('b', exclude=[itersym])

    blocked = set()
    for state in loop_states:
        for dn in state.data_nodes():
            if dn.data not in other_access_nodes:
                continue
            if dn.data not in write_set:
                continue
            if dn.data not in sdfg.arrays:
                continue
            desc = sdfg.arrays[dn.data]
            if not isinstance(desc, dace.data.Scalar) or not desc.transient:
                continue
            for e in state.in_edges(dn):
                if e.data.wcr is not None:
                    continue
                dst_subset = e.data.get_dst_subset(e, state)
                if not (dst_subset and _check_range(dst_subset, a, itersym,
                                                     b, step)):
                    blocked.add(dn.data)
    return blocked


def _find_enclosing_loop(sdfg: dace.SDFG,
                          inner: LoopRegion) -> LoopRegion | None:
    """Find the immediate parent LoopRegion of `inner`."""
    for cfg in sdfg.all_control_flow_regions():
        if not isinstance(cfg, LoopRegion):
            continue
        for child in cfg.nodes():
            if child is inner:
                return cfg
    return None


def _loops_sharing_range(parent: LoopRegion, start, end, step) -> list:
    """Find all direct-child LoopRegions of `parent` whose range matches."""
    matching = []
    for child in parent.nodes():
        if not isinstance(child, LoopRegion):
            continue
        info = _get_loop_range(child)
        if info is None:
            continue
        _, c_start, c_end, c_step = info
        if (symbolic.pystr_to_symbolic(c_start) ==
                symbolic.pystr_to_symbolic(start)
                and symbolic.pystr_to_symbolic(c_end) ==
                symbolic.pystr_to_symbolic(end)
                and symbolic.pystr_to_symbolic(c_step) ==
                symbolic.pystr_to_symbolic(step)):
            matching.append(child)
    return matching


def _all_accesses_in_compatible_loops(
        sdfg: dace.SDFG, parent: LoopRegion, name: str,
        compatible_loops: list[LoopRegion]) -> bool:
    """Check that ALL accesses to `name` within `parent` are inside one of
    the `compatible_loops` (or their nested children).

    If any access is in a state that's directly in the parent (not inside
    a compatible inner loop), return False — there's no itervar in scope
    to index the expanded array.
    """
    # States covered by compatible inner loops
    covered_states = set()
    for loop in compatible_loops:
        covered_states |= set(loop.all_states())

    # Check all states in parent
    for state in parent.all_states():
        if state in covered_states:
            continue
        # This state is in the parent but not inside any compatible loop
        for node in state.nodes():
            if isinstance(node, nd.AccessNode) and node.data == name:
                return False
    return True


def _expand_scalar(sdfg: dace.SDFG, name: str, dim_size, offset,
                    access_loops: list[LoopRegion]) -> bool:
    """Promote scalar `name` to a 1-D array and reindex all accesses.

    `dim_size` is the symbolic size of the new dimension.
    `offset` is the loop start value (for index computation: itervar - offset).
    `access_loops` are the LoopRegions whose bodies need reindexing.
    """
    if name not in sdfg.arrays:
        return False
    desc = sdfg.arrays[name]
    if not isinstance(desc, dace.data.Scalar):
        return False

    # Create new Array descriptor
    new_desc = dace.data.Array(
        dtype=desc.dtype,
        shape=[dim_size],
        transient=True,
        storage=desc.storage,
        lifetime=desc.lifetime,
    )
    # Replace the descriptor in-place
    sdfg.arrays[name] = new_desc

    # Update memlet subsets in all states within the access loops
    for loop in access_loops:
        itervar = loop.loop_variable
        itersym = symbolic.pystr_to_symbolic(itervar)
        idx_expr = itersym - symbolic.pystr_to_symbolic(offset)

        for state in loop.all_states():
            for edge in state.edges():
                if edge.data.data != name:
                    continue
                # Update subset: scalar [0] -> array [itervar - offset]
                new_range = sbs.Range([(idx_expr, idx_expr, 1)])
                edge.data.subset = new_range
                if edge.data.other_subset is not None:
                    edge.data.other_subset = new_range

    return True


def expand_scalars(sdfg: dace.SDFG) -> int:
    """Expand scalars that block LoopToMap in inner loops.

    Returns number of scalars expanded.
    """
    # Collect all LoopRegions
    all_loops: list[LoopRegion] = []
    for cfg in sdfg.all_control_flow_regions():
        for child in cfg.nodes():
            if isinstance(child, LoopRegion):
                all_loops.append(child)

    # For each inner loop, find blocked scalars and gather expansion info
    expansion_targets: dict[str, dict] = {}  # name -> expansion info

    for loop in all_loops:
        blocked = _blocked_scalars(sdfg, loop)
        if not blocked:
            continue

        info = _get_loop_range(loop)
        if info is None:
            continue
        itervar, start, end, step = info

        parent = _find_enclosing_loop(sdfg, loop)
        if parent is None:
            continue

        # Find sibling loops with same range
        siblings = _loops_sharing_range(parent, start, end, step)

        # Compute dimension size, ensuring only top-level symbols are used
        dim_size = (symbolic.pystr_to_symbolic(end)
                    - symbolic.pystr_to_symbolic(start)
                    + symbolic.pystr_to_symbolic(step))
        dim_size = sp.simplify(dim_size / symbolic.pystr_to_symbolic(step))

        # Check that dim_size only uses symbols already known at top level
        top_symbols = set(sdfg.symbols.keys()) | set(sdfg.constants.keys())
        dim_free = {str(s) for s in dim_size.free_symbols}
        new_symbols = dim_free - top_symbols
        if new_symbols:
            itervar_base = itervar.split('__')[0]
            dim_map = {'jl': 'klon', 'jk': 'klev', 'jm': 'nclv',
                       'jn': 'nclv', 'jo': 'nclv'}
            fallback = dim_map.get(itervar_base)
            if fallback and fallback in top_symbols:
                dim_size = symbolic.pystr_to_symbolic(fallback)
            else:
                continue

        for scalar_name in blocked:
            if scalar_name in expansion_targets:
                expansion_targets[scalar_name]['loops'].update(siblings)
            else:
                expansion_targets[scalar_name] = {
                    'start': start,
                    'dim_size': dim_size,
                    'offset': start,
                    'loops': set(siblings),
                    'parent': parent,
                }

    # Apply expansions (with safety check)
    expanded = 0
    skipped = 0
    for name, info in expansion_targets.items():
        if name not in sdfg.arrays:
            continue
        if not isinstance(sdfg.arrays[name], dace.data.Scalar):
            continue

        access_loops = list(info['loops'])
        parent = info['parent']

        # Safety: only expand if ALL accesses are inside compatible loops
        if not _all_accesses_in_compatible_loops(sdfg, parent, name,
                                                  access_loops):
            skipped += 1
            continue

        if _expand_scalar(sdfg, name, info['dim_size'], info['offset'],
                          access_loops):
            expanded += 1

    print(f"Scalar expansion: {expanded} scalars expanded"
          f" ({skipped} skipped — accesses outside inner loops)")
    return expanded


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scalar expansion for LoopToMap unblocking")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.input)
    expand_scalars(sdfg)

    out_path = args.output or args.input.replace(".sdfgz", "_expanded.sdfgz")
    sdfg.save(out_path)
    print(f"Saved to {out_path}")
