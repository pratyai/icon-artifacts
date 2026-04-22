"""array_privatization.py — Privatize small transient arrays with external init.

**Status: NOT enabled in optimize.py yet.** Correct (validates on cloudsc's
post-simplify SDFG) but does NOT unlock LoopToMap on its own, because
LoopToMap's race check is a purely static-subset analysis and doesn't care
about lifetime/scope. To actually unlock loops whose body writes a shared
transient at a constant index (e.g. cloudsc's `zvqx[1] = ...` inside a `jl`
loop), you need **array expansion** (widen the descriptor with a new
leading dim indexed by the loop's itervar) — which is not this pass.

What this pass does:
  - Detect transient arrays of small constant shape (product ≤ 16).
  - Find the *innermost* loop that contains writes to the array.
  - Mint a fresh same-shape transient with `Scope` lifetime.
  - Rename every reference inside that loop to the fresh name.
  - Clone the back-cone of every external write-AccessNode as an init state
    injected at the loop's start. Reader-only external states are left alone
    (they keep pointing at the original).

When it's useful:
  - Mostly as a building block. On its own it may help simplification passes
    see a transient as loop-local (since accesses from outside the loop no
    longer appear), but it won't make LoopToMap parallelize a loop whose
    memlets index the array by a constant.

Safety checks the pass performs:
  - `lifetime = AllocationLifetime.Scope` on the new descriptor.
  - Target-loop selection requires that the target loop actually *writes*
    the array (reader-only loops are skipped).
  - Init sources are limited to the back-cone of write-AccessNodes in each
    external state; unrelated nodes aren't cloned.

What it does **not** do:
  - Widen descriptor shape (that's expansion).
  - Remove the original descriptor — the original is still needed for any
    reader-only states that live outside the target loop.
  - Handle arrays with symbolic shape.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import dace
from dace import data as dt, nodes as nd
from dace.sdfg.state import LoopRegion, SDFGState

from ssa.graph_utils import (
    all_identifiers,
    collect_all_loops,
    is_write,
)

# A small array is worth privatizing; a big one would blow up memory if we
# per-thread'd it.  16 is arbitrary but comfortably above most cloudsc
# workspaces (nclv=5, ncldqi=2, ...).
MAX_EXPANSION_ELEMENTS = 16


def _is_small_constant_shape(desc: dt.Data) -> bool:
    """True if shape is all concrete ints and product <= MAX_EXPANSION_ELEMENTS."""
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


def _access_sites(sdfg: dace.SDFG, name: str) -> List[Tuple[dace.SDFG, SDFGState, nd.AccessNode]]:
    """Every (sub-sdfg, state, access-node) where `name` is accessed."""
    out = []
    for sd in sdfg.all_sdfgs_recursive():
        for st in sd.all_states():
            for n in st.nodes():
                if isinstance(n, nd.AccessNode) and n.data == name:
                    out.append((sd, st, n))
    return out


def _write_access_nodes(state: SDFGState, name: str) -> List[nd.AccessNode]:
    """AccessNodes in `state` for `name` that have at least one in-edge (are written)."""
    return [n for n in state.nodes()
            if isinstance(n, nd.AccessNode) and n.data == name and is_write(state, n)]


def _back_cone(state: SDFGState, sinks: List[nd.Node]) -> Set[nd.Node]:
    """All nodes reachable backward from any sink via in-edges (inclusive)."""
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


def _find_all_target_loops(sdfg: dace.SDFG, name: str
                           ) -> List[Tuple[LoopRegion, List[Tuple[SDFGState, List[nd.AccessNode]]]]]:
    """Return every (loop, inits) pair that should be privatized for `name`.

    Picks the *innermost* loop per access (smallest containing loop). If the
    same array is used in multiple disjoint loops (e.g. sibling branches of
    a conditional), each gets its own privatized copy.
    """
    top_sdfg = sdfg
    all_sites = _access_sites(top_sdfg, name)
    top_sites = [(sd, st, n) for (sd, st, n) in all_sites if sd is top_sdfg]
    if not top_sites:
        return []

    loops = collect_all_loops(top_sdfg)
    loop_states = {l: set(l.all_states()) for l in loops}
    touched_states = {st for (_, st, _) in top_sites}

    # Group write-accessed states by the *innermost* enclosing loop.
    # Only writes induce a race; reader-only states don't need per-iteration
    # copies and should keep seeing the original array.
    def _innermost(st: SDFGState) -> Optional[LoopRegion]:
        best: Optional[LoopRegion] = None
        best_size = None
        for loop in loops:
            if st in loop_states[loop]:
                if best is None or len(loop_states[loop]) < best_size:
                    best, best_size = loop, len(loop_states[loop])
        return best

    grouped: Dict[LoopRegion, Set[SDFGState]] = defaultdict(set)
    for _, st, _ in top_sites:
        if not _write_access_nodes(st, name):
            continue  # reader-only state doesn't pick a target loop
        inner = _innermost(st)
        if inner is not None:
            grouped[inner].add(st)

    plans = []
    for loop, in_loop_sts in grouped.items():
        # Init list: write-states that are OUTSIDE this loop.
        ls = loop_states[loop]
        inits: List[Tuple[SDFGState, List[nd.AccessNode]]] = []
        for st in touched_states:
            if st in ls:
                continue
            write_sinks = _write_access_nodes(st, name)
            if write_sinks:
                inits.append((st, write_sinks))
        if inits:
            plans.append((loop, inits))
    return plans


def _mint_name(sdfg: dace.SDFG, base: str, taken: Set[str]) -> str:
    """Fresh name `base__ap1`, `base__ap2`, ... with Scope lifetime so the
    storage is allocated freshly at map/scope entry (prevents per-iteration
    aliasing when the enclosing loop becomes a map)."""
    i = 1
    while True:
        cand = f"{base}__ap{i}"
        if cand not in taken:
            taken.add(cand)
            desc = copy.deepcopy(sdfg.arrays[base])
            desc.transient = True
            desc.lifetime = dace.dtypes.AllocationLifetime.Scope
            sdfg.add_datadesc(cand, desc)
            return cand
        i += 1


def _clone_back_cone_into(src_state: SDFGState, dst_state: SDFGState,
                          sinks: List[nd.Node], rename: Dict[str, str]) -> None:
    """Clone only the back-cone of `sinks` (inclusive) from `src_state` into
    `dst_state`, renaming access-node data refs."""
    cone = _back_cone(src_state, sinks)
    node_map: Dict[nd.Node, nd.Node] = {}
    for n in cone:
        clone = copy.deepcopy(n)
        if isinstance(clone, nd.AccessNode) and clone.data in rename:
            clone.data = rename[clone.data]
        dst_state.add_node(clone)
        node_map[n] = clone
    for e in src_state.edges():
        if e.src in cone and e.dst in cone:
            memlet = copy.deepcopy(e.data)
            if memlet.data in rename:
                memlet.data = rename[memlet.data]
            dst_state.add_edge(node_map[e.src], e.src_conn,
                               node_map[e.dst], e.dst_conn, memlet)


def _rename_inside_loop(loop: LoopRegion, rename: Dict[str, str]) -> None:
    """Rename every reference of data in `rename` inside `loop`'s subgraph.

    Uses DaCe's `replace_dict` which handles access nodes, memlets, interstate
    edges, tasklet code, and symbol mappings in NestedSDFGs.
    """
    loop.replace_dict(rename)


def _inject_init_into_loop_body(loop: LoopRegion,
                                inits: List[Tuple[SDFGState, List[nd.AccessNode]]],
                                rename: Dict[str, str]) -> None:
    """Insert a fresh state BEFORE the current start of `loop` containing
    cloned back-cones of every init's write sinks, with references renamed.
    Uses DaCe's `add_state_before` so the loop's start-block bookkeeping is
    updated correctly."""
    old_start = loop.start_block
    init_clone = loop.add_state_before(
        old_start,
        label=f"init_privatize_{next(iter(rename.values()))}",
        is_start_block=True,
    )

    for src_state, sinks in inits:
        _clone_back_cone_into(src_state, init_clone, sinks, rename)


def privatize_arrays(sdfg: dace.SDFG) -> int:
    """Apply array privatization for all qualifying (loop, array) pairs."""
    n_applied = 0
    taken = all_identifiers(sdfg)

    # Only candidate arrays: transient, small constant shape, at top-level SDFG.
    candidates = [name for name, desc in list(sdfg.arrays.items())
                  if desc.transient and _is_small_constant_shape(desc)]

    for name in candidates:
        plans = _find_all_target_loops(sdfg, name)
        for loop, inits in plans:
            new_name = _mint_name(sdfg, name, taken)
            rename = {name: new_name}
            _rename_inside_loop(loop, rename)
            _inject_init_into_loop_body(loop, inits, rename)
            n_applied += 1
            print(f"  array-privatize: {name} -> {new_name}  "
                  f"(loop={loop.label}, inits={len(inits)})")

    if n_applied:
        print(f"Array privatization: {n_applied} arrays privatized")
    return n_applied


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Privatize small arrays inside loops")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    args = parser.parse_args()
    sdfg = dace.SDFG.from_file(args.input)
    privatize_arrays(sdfg)
    out_path = args.output or args.input.replace(".sdfgz", "_arrpriv.sdfgz")
    sdfg.save(out_path, compress=True)
    print(f"Saved to {out_path}")
