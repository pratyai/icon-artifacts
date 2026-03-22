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
from dace import nodes as nd, data
from dace.sdfg.state import SDFGState, LoopRegion, ControlFlowBlock

from ssa.graph_utils import is_write


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


def _loop_sites(loop: LoopRegion):
    """Return (states, blocks, edges) sets for everything inside the loop."""
    states = set(loop.all_states())
    blocks = set(loop.all_control_flow_blocks())
    edges = {e.data for e, _ in loop.all_edges_recursive()
             if isinstance(e.data, dace.InterstateEdge)}
    return states, blocks, edges


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


def _written_scalars(sdfg: dace.SDFG, loop: LoopRegion) -> set[str]:
    """Return transient scalar names written inside the loop."""
    written = set()
    for state in loop.all_states():
        for node in state.nodes():
            if isinstance(node, nd.AccessNode) and is_write(state, node):
                name = node.data
                if (name in sdfg.arrays and
                    isinstance(sdfg.arrays[name], data.Scalar) and
                    sdfg.arrays[name].transient):
                    written.add(name)
    return written


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

    loops: List[LoopRegion] = []
    for sd in sdfg.all_sdfgs_recursive():
        for block in sd.all_control_flow_blocks():
            if isinstance(block, LoopRegion):
                loops.append(block)
    if not loops:
        return []

    loops.sort(key=lambda l: len(list(l.all_states())))

    plan = []
    counter = defaultdict(int)
    for loop in loops:
        ls, lb, le = _loop_sites(loop)
        repl = {}
        for name in _written_scalars(sdfg, loop):
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
