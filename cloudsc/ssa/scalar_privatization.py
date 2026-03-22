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

from ssa.graph_utils import collect_all_loops, loop_interior, written_transient_scalars


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


def _mint_name(sdfg: dace.SDFG, base: str, counter: dict[str, int]) -> str:
    """Create a fresh name like base__p1 and register the descriptor."""
    counter[base] += 1
    name = f"{base}__p{counter[base]}"
    while name in sdfg.arrays or name in sdfg.symbols or name in sdfg.constants:
        counter[base] += 1
        name = f"{base}__p{counter[base]}"
    desc = copy.deepcopy(sdfg.arrays[base])
    desc.transient = True
    sdfg.add_datadesc(name, desc)
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
    """Phase 1: Identify all privatization renames (inner-loops-first)."""
    refs = _collect_refs(sdfg)

    loops = collect_all_loops(sdfg)
    if not loops:
        return []

    loops.sort(key=lambda l: len(list(l.all_states())))

    plan = []
    counter = defaultdict(int)
    for loop in loops:
        ls, lb, le = loop_interior(loop)
        repl = {}
        for name in written_transient_scalars(sdfg, ls):
            if _is_private(refs, name, ls, lb, le):
                repl[name] = _mint_name(sdfg, name, counter)
        if repl:
            plan.append((loop, repl))
            for name in repl:
                _drop_loop_refs(refs, name, ls, lb, le)
    return plan


def apply_plan(plan: List[Tuple[LoopRegion, Dict[str, str]]]):
    """Phase 2: Execute all renames."""
    for loop, repl in plan:
        loop.replace_dict(repl)


def privatize_scalars(sdfg: dace.SDFG) -> int:
    """Replace loop-private scalars with fresh transients. Returns count."""
    p = build_plan(sdfg)
    apply_plan(p)
    n = sum(len(r) for _, r in p)
    if n:
        print(f"Scalar privatization: {n} scalars privatized")
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
