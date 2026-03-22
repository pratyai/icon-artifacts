"""data_ssa.py — SSA for data descriptors (Scalars) in DaCe SDFGs.

Renames transient scalars that are written more than once so that each
write site produces a unique variable.  Reads are updated to reference the
version that *reaches* them (reaching-definition analysis on the CFG).
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Set, List, Dict, Optional, Tuple, Any, Union

from tqdm import tqdm
import dace
from dace import nodes as nd, symbolic
from dace.sdfg.state import ConditionalBlock, LoopRegion, ControlFlowRegion, SDFGState, ControlFlowBlock

from ssa.graph_utils import (
    rename_access_node_and_tree, update_node_content,
    update_interstate_edge, update_metadata, is_write, is_read
)
from ssa.ssa_analysis import SSAVersionMap, SSAPlan, analyze_cfg, SSAVersion


# ---------------------------------------------------------------------------
# metadata helpers
# ---------------------------------------------------------------------------

def _collect_multi_write_transients(sdfg: dace.SDFG) -> set[str]:
    """Return names of transient scalars written more than once."""
    write_counts: dict[str, int] = defaultdict(int)
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nd.AccessNode) and is_write(state, node):
                write_counts[node.data] += 1
        
        for edge in state.edges():
            if edge.data.data and not isinstance(edge.dst, nd.AccessNode):
                write_counts[edge.data.data] += 1

    return {
        name for name, count in write_counts.items()
        if count > 1 and name in sdfg.arrays and sdfg.arrays[name].transient
        and isinstance(sdfg.arrays[name], dace.data.Scalar)
    }


def _mint_name(sdfg: dace.SDFG, base: str, counter: dict[str, int]) -> str:
    """Create a fresh SSA name like ``base__v2`` and register the descriptor."""
    new_base = base.split('__')[0]
    counter[new_base] = counter.get(new_base, 0) + 1
    ver = counter[new_base]
    new_name = f"{new_base}__v{ver}"
    while new_name in sdfg.arrays or new_name in sdfg.symbols or new_name in sdfg.constants:
        ver += 1; counter[new_base] = ver; new_name = f"{new_base}__v{ver}"
    
    orig = sdfg.arrays[base]
    new_desc = copy.deepcopy(orig)
    new_desc.transient = True
    sdfg.add_datadesc(new_name, new_desc)
    return new_name


def _create_identifier_dict(sdfg: dace.SDFG) -> set[tuple[str, dace.SDFG]]:
    """Create a dict of all identifiers used in the SDFG, mapping name → parent SDFG."""
    identifiers: set[tuple[str, dace.SDFG]] = set()
    for g in sdfg.all_sdfgs_recursive():
        for a in g.arrays: identifiers.add((a, g))
        for s in g.symbols: identifiers.add((s, g))
        for c in g.constants: identifiers.add((c, g))
        for edge in g.edges():
            for sym in edge.data.assignments: identifiers.add((sym, g))
    return identifiers


# ---------------------------------------------------------------------------
# transformation pass
# ---------------------------------------------------------------------------

def _apply_plan(sdfg: dace.SDFG, plan: SSAPlan, counter: dict[str, int]):
    """Apply a planned set of renames to the SDFG hierarchy."""
    
    for ver in tqdm(plan.versions, desc="Applying SSA plan", leave=False):
        new_name = _mint_name(sdfg, ver.base_name, counter)
        
        # 2. Rename all AccessNodes
        for state, node in ver.access_nodes:
            rename_access_node_and_tree(state, node, ver.base_name, new_name)
        
        # 3. Update symbolic usages
        repl = {ver.base_name: new_name}
        for obj in ver.symbol_usages:
            if isinstance(obj, nd.Node):
                update_node_content(obj, repl)
                state = getattr(obj, 'parent_graph', None)
                if isinstance(state, SDFGState):
                    for e in state.edges():
                        if (e.src == obj or e.dst == obj) and e.data.data == ver.base_name:
                            if not isinstance(e.src, nd.AccessNode) and not isinstance(e.dst, nd.AccessNode):
                                e.data.data = new_name
            elif isinstance(obj, dace.InterstateEdge):
                update_interstate_edge(obj, repl)
            elif isinstance(obj, ControlFlowBlock):
                update_metadata(obj, repl)


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

def _cleanup_unused_descriptors(sdfg: dace.SDFG):
    """Remove SSA descriptors (__v*) that are no longer referenced."""
    ssa_pattern = re.compile(r'__v\d+$')
    used: Set[str] = set()
    all_sdfgs = list(sdfg.all_sdfgs_recursive())
    
    for sd in all_sdfgs:
        for block in sd.all_control_flow_blocks():
            used.update(str(s) for s in block.free_symbols)
            if isinstance(block, SDFGState):
                for node in block.nodes():
                    if isinstance(node, nd.AccessNode): used.add(node.data)
                    else: used.update(str(s) for s in node.free_symbols)
                    if isinstance(node, nd.NestedSDFG):
                        used.update(node.in_connectors.keys())
                        used.update(node.out_connectors.keys())
                        for expr in node.symbol_mapping.values():
                            used.update(str(s) for s in symbolic.free_symbols_and_functions(symbolic.pystr_to_symbolic(expr)))
                for edge in block.edges():
                    if edge.data.data: used.add(edge.data.data)
        
        for edge, _ in sd.all_edges_recursive():
            if isinstance(edge.data, dace.InterstateEdge):
                used.update(str(s) for s in edge.data.free_symbols)
                used.update(edge.data.assignments.keys())

    total_removed = 0
    for sd in all_sdfgs:
        to_remove = [n for n in list(sd.arrays.keys()) if ssa_pattern.search(n) and n not in used]
        for name in to_remove: del sd.arrays[name]
        total_removed += len(to_remove)
    return total_removed


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def ssa_transform_wtr(sdfg: dace.SDFG):
    """SSA transform for write-then-read variables (always written before read)."""
    targets = _collect_always_write_then_read_transients(sdfg)
    if not targets:
        print("No always-write-then-read transient scalars found.")
        return
    print(f"SSA candidates: {len(targets)} transient scalars always written before read")
    identifiers = _create_identifier_dict(sdfg)
    counter: dict[str, int] = {name: 0 for name, _ in identifiers}
    
    accs = [(acc, state) for acc, state in sdfg.all_nodes_recursive() 
            if isinstance(acc, nd.AccessNode) and (acc.data, state.sdfg) in targets]
    
    for acc, state in accs:
        orig_name = acc.data
        new_name = _mint_name(state.sdfg, orig_name, counter)
        rename_access_node_and_tree(state, acc, orig_name, new_name)
        for cb in state.sdfg.all_control_flow_blocks():
            if isinstance(cb, ConditionalBlock):
                update_metadata(cb, {orig_name: new_name})
        print(f"Renamed {orig_name} to {new_name} in state {state.label}")


def ssa_transform(sdfg: dace.SDFG, only: set[str] | None = None) -> dict[str, list[str]]:
    """Apply SSA renaming to all multi-write transients."""
    all_sdfgs = list(sdfg.all_sdfgs_recursive())
    counter: dict[str, int] = {}
    total_versioned_vars = 0

    targets_per_sdfg = {}
    for sd in all_sdfgs:
        targets = {n for n in only if n in sd.arrays and isinstance(sd.arrays[n], dace.data.Scalar)} if (only and sd == sdfg) else _collect_multi_write_transients(sd)
        if targets:
            targets_per_sdfg[sd] = targets
            print(f"SSA candidates in {sd.label}: {len(targets)} transient variables")
            vmap = SSAVersionMap()
            plan = SSAPlan()
            analyze_cfg(sd, vmap, targets, plan)
            _apply_plan(sd, plan, counter)
            total_versioned_vars += len(targets)

    removed = _cleanup_unused_descriptors(sdfg)
    
    top_level_result: dict[str, list[str]] = {}
    if sdfg in targets_per_sdfg:
        for base in targets_per_sdfg[sdfg]:
            pattern = re.compile(r'^' + re.escape(base) + r'__v\d+$')
            versions = sorted(name for name in sdfg.arrays if pattern.match(name))
            if versions: top_level_result[base] = versions
    
    total_versions_created = sum(1 for sd in all_sdfgs for name in sd.arrays if "__v" in name)
    print(f"Total: Created {total_versions_created} SSA versions for {total_versioned_vars} variables")
    print(f"Removed {removed} unused descriptors")
    return top_level_result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SSA transform for DaCe SDFGs")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    parser.add_argument("--only", type=str, default=None)
    args = parser.parse_args()
    sdfg = dace.SDFG.from_file(args.input)
    ssa_transform(sdfg, only=set(args.only.split(",")) if args.only else None)
    out_path = args.output or args.input.replace(".sdfgz", "_ssa.sdfgz")
    sdfg.save(out_path)
    print(f"Saved to {out_path}")
