"""scalar_privatization.py — Replace loop-private scalars with fresh transients.

For each LoopRegion, finds scalars that are:
  - Written inside the loop.
  - NOT accessed outside the loop (loop-private).

Replaces them with a fresh transient unique to that loop.

Two-phase: analyze (collect refs, build rename plan) then apply (replace_dict).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Set, Dict, List, Tuple, Any

import dace
from dace import nodes as nd
from dace.sdfg.state import SDFGState, LoopRegion, ControlFlowBlock

from ssa.graph_utils import collect_all_loops, loop_interior, written_transient_scalars, loop_carried_scalars, all_identifiers, fresh_name


def _collect_refs(sdfg: dace.SDFG) -> Dict[str, Set[Any]]:
    """Map variable name → set of sites (SDFGState | ControlFlowBlock | InterstateEdge)."""
    refs = defaultdict(set)
    for sd in sdfg.all_sdfgs_recursive():
        for block in sd.all_control_flow_blocks():
            for sym in block.free_symbols:
                refs[str(sym)].add(block)
            if isinstance(block, SDFGState):
                for node in block.nodes():
                    if isinstance(node, nd.AccessNode):
                        refs[node.data].add(block)
                    else:
                        for sym in node.free_symbols:
                            refs[str(sym)].add(block)
                for edge in block.edges():
                    if edge.data.data:
                        refs[edge.data.data].add(block)
        for edge, _ in sd.all_edges_recursive():
            if isinstance(edge.data, dace.InterstateEdge):
                for sym in edge.data.free_symbols:
                    refs[str(sym)].add(edge.data)
                for k in edge.data.assignments:
                    refs[k].add(edge.data)
    return refs


def _is_private(refs, name, loop_states, loop_blocks, loop_edges) -> bool:
    """True if all references to `name` are inside the loop."""
    if name not in refs:
        return True
    for site in refs[name]:
        if isinstance(site, SDFGState):
            if site not in loop_states: return False
        elif isinstance(site, ControlFlowBlock):
            if site not in loop_blocks: return False
        elif isinstance(site, dace.InterstateEdge):
            if site not in loop_edges: return False
        else:
            return False
    return True


def _strip_p_suffix(name: str) -> str:
    """Strip __pN suffixes to get the original base name."""
    import re
    return re.sub(r'(__p\d+)+$', '', name)


def _mint_name(sdfg: dace.SDFG, base: str, counter: dict[str, int],
               taken: set[str] | None = None) -> str:
    """Create a fresh name like base__p1 and register the descriptor."""
    if taken is None:
        taken = all_identifiers(sdfg)
    root = _strip_p_suffix(base)
    counter[root] += 1
    name = f"{root}__p{counter[root]}"
    while name in taken:
        counter[root] += 1
        name = f"{root}__p{counter[root]}"
    desc = copy.deepcopy(sdfg.arrays[base])
    desc.transient = True
    sdfg.add_datadesc(name, desc)
    taken.add(name)
    return name


def _drop_loop_refs(refs, name, loop_states, loop_blocks, loop_edges):
    """Remove loop-internal refs for `name` so outer loops see it as private."""
    if name not in refs:
        return
    refs[name] = {
        s for s in refs[name]
        if not ((isinstance(s, SDFGState) and s in loop_states) or
                (isinstance(s, ControlFlowBlock) and s in loop_blocks) or
                (isinstance(s, dace.InterstateEdge) and s in loop_edges))
    }
    if not refs[name]:
        del refs[name]


# ---------------------------------------------------------------------------
# Two-phase entry points
# ---------------------------------------------------------------------------

def build_plan(sdfg: dace.SDFG) -> List[Tuple[LoopRegion, Dict[str, str]]]:
    """Phase 1: Identify all privatization renames (inner-loops-first).

    Two passes:
      1. Strictly private scalars (all refs inside one loop).
      2. Shared-but-safe scalars: written in multiple loops but
         write-before-read in each (not loop-carried), so each loop
         can safely get its own copy.
    """
    refs = _collect_refs(sdfg)

    loops = collect_all_loops(sdfg)
    if not loops:
        return []

    loops.sort(key=lambda l: len(list(l.all_states())))

    plan = []
    counter = defaultdict(int)
    taken = all_identifiers(sdfg)

    # Pass 1: strictly private
    for loop in loops:
        ls, lb, le = loop_interior(loop)
        repl = {}
        for name in written_transient_scalars(sdfg, ls):
            if _is_private(refs, name, ls, lb, le):
                repl[name] = _mint_name(sdfg, name, counter, taken)
        if repl:
            plan.append((loop, repl))
            for name in repl:
                _drop_loop_refs(refs, name, ls, lb, le)

    # Pass 2: shared-but-safe — written in multiple loops but
    # write-before-read in each, so privatization per loop is safe.
    # Only if ALL external refs are inside other loops that also write
    # the scalar (i.e., no bare reads, no metadata/edge refs outside loops).
    all_loop_interiors = {}
    for loop in loops:
        all_loop_interiors[id(loop)] = loop_interior(loop)

    all_loop_states = set()
    all_loop_blocks = set()
    all_loop_edges = set()
    for lid, (ls, lb, le) in all_loop_interiors.items():
        all_loop_states |= ls
        all_loop_blocks |= lb
        all_loop_edges |= le

    def _all_external_refs_in_loops(refs, name):
        """True if every ref outside the current loop is inside some other loop."""
        if name not in refs:
            return True
        for site in refs[name]:
            if isinstance(site, SDFGState):
                if site not in all_loop_states:
                    return False
            elif isinstance(site, ControlFlowBlock):
                if site not in all_loop_blocks:
                    return False
            elif isinstance(site, dace.InterstateEdge):
                if site not in all_loop_edges:
                    return False
            else:
                return False
        return True

    for loop in loops:
        ls, lb, le = all_loop_interiors[id(loop)]
        carried = loop_carried_scalars(sdfg, loop)
        repl = {}
        for name in written_transient_scalars(sdfg, ls):
            if name in carried:
                continue
            if _is_private(refs, name, ls, lb, le):
                continue
            if not _all_external_refs_in_loops(refs, name):
                continue
            repl[name] = _mint_name(sdfg, name, counter, taken)
        if repl:
            plan.append((loop, repl))
            for name in repl:
                _drop_loop_refs(refs, name, ls, lb, le)

    return plan


def apply_plan(plan: List[Tuple[LoopRegion, Dict[str, str]]]):
    """Phase 2: Execute all renames."""
    for loop, repl in plan:
        loop.replace_dict(repl)


def _build_pass3(sdfg: dace.SDFG, counter: dict[str, int],
                 taken: set[str] | None = None) -> List[Tuple[LoopRegion, Dict[str, str]]]:
    """Pass 3: re-privatize scalars shared by sibling loops.

    After passes 1+2 rename a scalar at a parent loop, all inner loops
    share the same __pN name. This pass gives each inner loop its own copy
    if the scalar is written (write-before-read) in each.
    """
    if taken is None:
        taken = all_identifiers(sdfg)
    refs = _collect_refs(sdfg)
    loops = collect_all_loops(sdfg)
    if not loops:
        return []
    loops.sort(key=lambda l: len(list(l.all_states())))

    all_loop_states = set()
    all_loop_blocks = set()
    all_loop_edges = set()
    for loop in loops:
        ls, lb, le = loop_interior(loop)
        all_loop_states |= ls
        all_loop_blocks |= lb
        all_loop_edges |= le

    def _all_ext_in_loops(name):
        if name not in refs:
            return True
        for site in refs[name]:
            if isinstance(site, SDFGState):
                if site not in all_loop_states: return False
            elif isinstance(site, ControlFlowBlock):
                if site not in all_loop_blocks: return False
            elif isinstance(site, dace.InterstateEdge):
                if site not in all_loop_edges: return False
            else:
                return False
        return True

    plan = []
    for loop in loops:
        ls, lb, le = loop_interior(loop)
        carried = loop_carried_scalars(sdfg, loop)
        repl = {}
        for name in written_transient_scalars(sdfg, ls):
            if name in carried:
                continue
            if _is_private(refs, name, ls, lb, le):
                continue
            if not _all_ext_in_loops(name):
                continue
            repl[name] = _mint_name(sdfg, name, counter, taken)
        if repl:
            plan.append((loop, repl))
            for name in repl:
                _drop_loop_refs(refs, name, ls, lb, le)

    return plan


def privatize_scalars(sdfg: dace.SDFG) -> int:
    """Replace loop-private scalars with fresh transients. Returns count."""
    p = build_plan(sdfg)
    apply_plan(p)
    n = sum(len(r) for _, r in p)

    # Pass 3: re-privatize after passes 1+2 applied
    counter = defaultdict(int)
    # Seed counter from existing __pN names to avoid collisions
    for name in sdfg.arrays:
        if '__p' in name:
            root = _strip_p_suffix(name)
            _, _, num = name.rpartition('__p')
            if num.isdigit():
                counter[root] = max(counter[root], int(num))
    taken = all_identifiers(sdfg)
    p3 = _build_pass3(sdfg, counter, taken)
    apply_plan(p3)
    n3 = sum(len(r) for _, r in p3)
    n += n3

    if n:
        print(f"Scalar privatization: {n} scalars privatized ({n3} in pass 3)")
    return n


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scalar privatization")
    parser.add_argument("input", help="Input SDFG file")
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()
    sdfg = dace.SDFG.from_file(args.input)
    privatize_scalars(sdfg)
    out = args.output or args.input.replace(".sdfgz", "_priv.sdfgz")
    sdfg.save(out)
    print(f"Saved to {out}")
