"""array_expansion.py — Widen small transient arrays with a loop-itervar dim.

**Narrow scope.** Targets the cloudsc `zvqx` pattern:

    zvqx = np.ndarray((nclv,))           # declared at top level
    zvqx[0] = 0.0                        # INIT state(s), outside the target loop
    ...
    for jk: for jl:                      # target loop is innermost writer of zvqx
        zvqx[1] = f(jl)                  # write with loop-invariant subset
        zfall = zvqx[jm-1] * ...         # read

The writes use an index that does NOT depend on `jl`, so LoopToMap rejects
the jl loop ("LOOP_INVARIANT_ARRAY"). Privatization with `lifetime=Scope` —
see `array_privatization.py` — doesn't help because LoopToMap's race check
is static-subset-based and doesn't know about lifetime.

This pass widens the array to `(loop_extent, *orig_shape)` so every subset
inside the loop grows a leading dim indexed by the itervar. Writes become
`[jl - start, 1]`, reads become `[jl - start, jm-1]`. Each jl now writes
a distinct cell; LoopToMap accepts.

What it does NOT handle:
  - Arrays that are read outside the target loop (those external reads
    would need a decision: keep the original array, pick a fixed row, or
    merge post-loop). Currently we keep the original for any such readers
    and only create a new expanded copy for in-loop use.
  - Symbolic-shape arrays.
  - Deeply nested loops — we pick one target loop per array.
  - NestedSDFG inner descriptors: we slice at the outer memlet
    (`expanded[jl, :]`), leaving the inner descriptor's shape unchanged.
  - Loops with non-linear or reversed strides.

Safety checks:
  - Only transient arrays with all-constant small shape (product ≤ 16).
  - Only arrays whose in-loop writes have subsets that don't reference the
    target loop's itervar (otherwise expansion is pointless).
  - External write sites must be cloneable back-cones (same as
    array_privatization).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import dace
from dace import data as dt, nodes as nd, subsets as sbs, symbolic
from dace.sdfg.state import LoopRegion, SDFGState
from dace.transformation.passes.analysis import loop_analysis

from ssa.graph_utils import (
    all_identifiers,
    collect_all_loops,
    is_write,
)

MAX_EXPANSION_ELEMENTS = 16


def _is_small_constant_shape(desc: dt.Data) -> bool:
    if not isinstance(desc, dt.Array):
        return False
    try:
        dims = [int(d) for d in desc.shape]
    except (TypeError, ValueError):
        return False
    if any(d <= 0 for d in dims):
        return False
    prod = 1
    for d in dims:
        prod *= d
    return prod <= MAX_EXPANSION_ELEMENTS


def _loop_range(loop: LoopRegion):
    start = loop_analysis.get_init_assignment(loop)
    end = loop_analysis.get_loop_end(loop)
    step = loop_analysis.get_loop_stride(loop)
    itervar = loop.loop_variable
    if any(x is None for x in (start, end, step, itervar)):
        return None
    # DaCe's get_loop_end returns the *inclusive* end of the iteration range
    # (e.g. for `jl <= kfdia` it returns `kfdia`). Callers expect extent to be
    # computed as `end - start + 1`.
    return itervar, start, end, step


def _access_sites(sdfg: dace.SDFG, name: str):
    out = []
    for sd in sdfg.all_sdfgs_recursive():
        for st in sd.all_states():
            for n in st.nodes():
                if isinstance(n, nd.AccessNode) and n.data == name:
                    out.append((sd, st, n))
    return out


def _write_access_nodes(state: SDFGState, name: str) -> List[nd.AccessNode]:
    return [n for n in state.nodes()
            if isinstance(n, nd.AccessNode) and n.data == name and is_write(state, n)]


def _back_cone(state: SDFGState, sinks: List[nd.Node]) -> Set[nd.Node]:
    seen: Set[nd.Node] = set()
    stack = list(sinks)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        for e in state.in_edges(n):
            stack.append(e.src)
    return seen


def _references_itervar(subset, itervar: str) -> bool:
    """True if any range in the subset mentions `itervar`."""
    if subset is None:
        return False
    try:
        for rng in subset:
            for sym in rng:
                if sym is None:
                    continue
                if itervar in str(sym):
                    return True
    except TypeError:
        pass
    try:
        return itervar in str(subset)
    except Exception:
        return False


def _find_target_loops(sdfg: dace.SDFG, name: str
                       ) -> List[Tuple[LoopRegion, List[Tuple[SDFGState, List[nd.AccessNode]]]]]:
    """Every (loop, inits) where `name` is written inside `loop` with subsets
    that DON'T already depend on `loop.loop_variable` (so expansion is useful).
    """
    all_sites = _access_sites(sdfg, name)
    top_sites = [(sd, st, n) for (sd, st, n) in all_sites if sd is sdfg]
    if not top_sites:
        return []

    loops = collect_all_loops(sdfg)
    loop_states = {l: set(l.all_states()) for l in loops}
    touched_states = {st for (_, st, _) in top_sites}

    def _innermost(st: SDFGState) -> Optional[LoopRegion]:
        best: Optional[LoopRegion] = None
        best_size: Optional[int] = None
        for loop in loops:
            if st in loop_states[loop]:
                if best is None or len(loop_states[loop]) < (best_size or 0):
                    best, best_size = loop, len(loop_states[loop])
        return best

    # Group write-states by innermost loop.
    grouped: Dict[LoopRegion, Set[SDFGState]] = defaultdict(set)
    for _, st, _ in top_sites:
        if not _write_access_nodes(st, name):
            continue
        inner = _innermost(st)
        if inner is not None:
            grouped[inner].add(st)

    plans = []
    for loop, write_states in grouped.items():
        info = _loop_range(loop)
        if info is None:
            continue
        itervar, _, _, _ = info

        # Check at least one in-loop write has a subset that does NOT reference
        # `itervar` — otherwise expansion wouldn't change anything useful.
        ls = loop_states[loop]
        useful = False
        for st in write_states:
            for n in _write_access_nodes(st, name):
                for e in st.in_edges(n):
                    if not _references_itervar(e.data.subset, itervar):
                        useful = True
                        break
                if useful:
                    break
            if useful:
                break
        if not useful:
            continue

        # Collect init states (write-states outside this loop).
        inits: List[Tuple[SDFGState, List[nd.AccessNode]]] = []
        for st in touched_states:
            if st in ls:
                continue
            ws = _write_access_nodes(st, name)
            if ws:
                inits.append((st, ws))
        if inits:
            plans.append((loop, inits))

    return plans


def _mint_expanded(sdfg: dace.SDFG, base: str, extent, taken: Set[str]) -> str:
    """Mint a fresh expanded transient: shape = (extent, *orig.shape)."""
    orig = sdfg.arrays[base]
    i = 1
    while True:
        cand = f"{base}__ex{i}"
        if cand not in taken:
            taken.add(cand)
            new_shape = tuple([extent] + list(orig.shape))
            sdfg.add_transient(cand, new_shape, orig.dtype,
                               lifetime=dace.dtypes.AllocationLifetime.Scope,
                               find_new_name=False)
            return cand
        i += 1


def _prepend_subset(sub, new_index) -> sbs.Range:
    """Return a Range equal to `sub` with `new_index` prepended as a new dim.

    Accepts either a Range or Indices; returns Range for uniformity.
    """
    from dace.subsets import Range, Indices
    if sub is None:
        # scalar-like access; only the new dim exists
        return Range([(new_index, new_index, 1)])
    if isinstance(sub, Indices):
        sub = Range([(s, s, 1) for s in sub])
    assert isinstance(sub, Range)
    prefix = [(new_index, new_index, 1)]
    return Range(prefix + list(sub.ranges))


def _rewrite_memlet_subsets(memlet: dace.Memlet, new_name: str, new_index) -> None:
    """Rewrite `memlet` so it references `new_name`, prepending `new_index` to
    the *primary* subset (which addresses the expanded endpoint).  `other_subset`
    addresses the other endpoint (usually a Tasklet connector or an AccessNode
    with the original shape), so we don't touch it."""
    memlet.data = new_name
    if memlet.subset is not None:
        memlet.subset = _prepend_subset(memlet.subset, new_index)


def _expand_in_loop(loop: LoopRegion, old_name: str, new_name: str, itervar_expr) -> int:
    """Walk every state inside `loop`; rewrite any AccessNode+memlet referring
    to `old_name` to `new_name` with a prepended `itervar_expr` dim.
    Returns number of memlets rewritten."""
    n = 0
    for st in loop.all_states():
        for node in list(st.nodes()):
            if isinstance(node, nd.AccessNode) and node.data == old_name:
                node.data = new_name
        for e in st.edges():
            if e.data.data == old_name:
                _rewrite_memlet_subsets(e.data, new_name, itervar_expr)
                n += 1
    return n


def _clone_init_into_loop(loop: LoopRegion,
                          inits: List[Tuple[SDFGState, List[nd.AccessNode]]],
                          old_name: str, new_name: str, itervar_expr) -> None:
    """Clone each init's write back-cone into a fresh state inserted BEFORE
    the current start of `loop`, rewriting AccessNodes and memlets to target
    `new_name[itervar, ...]`. Uses DaCe's `add_state_before` so the loop's
    start-block bookkeeping is updated correctly."""
    old_start = loop.start_block
    init_state = loop.add_state_before(
        old_start,
        label=f"init_expand_{new_name}",
        is_start_block=True,
    )

    for src_state, sinks in inits:
        cone = _back_cone(src_state, sinks)
        node_map: Dict[nd.Node, nd.Node] = {}
        for n in cone:
            clone = copy.deepcopy(n)
            if isinstance(clone, nd.AccessNode) and clone.data == old_name:
                clone.data = new_name
            init_state.add_node(clone)
            node_map[n] = clone
        for e in src_state.edges():
            if e.src in cone and e.dst in cone:
                memlet = copy.deepcopy(e.data)
                if memlet.data == old_name:
                    _rewrite_memlet_subsets(memlet, new_name, itervar_expr)
                init_state.add_edge(node_map[e.src], e.src_conn,
                                    node_map[e.dst], e.dst_conn, memlet)


def expand_arrays(sdfg: dace.SDFG) -> int:
    """Top-level: widen qualifying small transient arrays by one loop-itervar dim.

    Returns number of (name, loop) pairs expanded.
    """
    n_applied = 0
    taken = all_identifiers(sdfg)

    candidates = [name for name, desc in list(sdfg.arrays.items())
                  if desc.transient and _is_small_constant_shape(desc)]

    for name in candidates:
        plans = _find_target_loops(sdfg, name)
        for loop, inits in plans:
            info = _loop_range(loop)
            if info is None:
                continue
            itervar, start, end, step = info

            # Extent for the new leading dim: end - start + 1 (inclusive end).
            # We use `(itervar - start)` as the offset when indexing; it ranges
            # 0 .. end-start, i.e. exactly `extent` distinct values.
            extent = symbolic.pystr_to_symbolic(f"({end}) - ({start}) + 1")
            itervar_offset = symbolic.pystr_to_symbolic(f"({itervar}) - ({start})")

            new_name = _mint_expanded(sdfg, name, extent, taken)

            # Rewrite inside the loop.
            n_rewritten = _expand_in_loop(loop, name, new_name, itervar_offset)

            # Clone init.
            _clone_init_into_loop(loop, inits, name, new_name, itervar_offset)

            n_applied += 1
            print(f"  array-expand: {name} -> {new_name}  "
                  f"(loop={loop.label}, itervar={itervar}, extent={extent}, "
                  f"rewritten={n_rewritten}, inits={len(inits)})")

    if n_applied:
        print(f"Array expansion: {n_applied} (array, loop) pairs expanded")
    return n_applied


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Expand small arrays by a loop-itervar dim")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    args = parser.parse_args()
    sdfg = dace.SDFG.from_file(args.input)
    expand_arrays(sdfg)
    out_path = args.output or args.input.replace(".sdfgz", "_arrexp.sdfgz")
    sdfg.save(out_path, compress=True)
    print(f"Saved to {out_path}")
