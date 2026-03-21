"""data_ssa.py — SSA for data descriptors (Scalars) in DaCe SDFGs.

Renames transient scalars that are written more than once so that each
write site produces a unique variable.  Reads are updated to reference the
version that *reaches* them (reaching-definition analysis on the CFG).

This enables downstream optimisations (dead-store elimination, better fusion,
scalar replacement) that are blocked when a single name is reused for
logically independent values.

Usage:
    python -m ssa.data_ssa cloudsc_pydace_simplified_symbolic.sdfgz -o cloudsc_ssa.sdfgz

Algorithm (simplified, for structured CFGs — no goto):
    1. Walk the CFG in topological order (recursive into loops / conditionals).
    2. Maintain a *version map*: var → current SSA name.
    3. At every **write** AccessNode, mint a fresh SSA name, register the new
       array/scalar descriptor (clone of original), and rewrite the node + edges.
    4. At every **read** AccessNode, rewrite to the current SSA name from the map.
    5. Loops: snapshot the version map at entry.  After processing the body,
       detect **loop-carried** variables (version at body-end != version at
       body-start AND the variable is read before it is first written in the body).
       For those, unify back to the entry version (keep single name across iterations).
    6. Conditionals: process each branch independently.  After the conditional,
       if both branches agree on the same version, keep it; otherwise create a
       copy-merge state (trivial phi).
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict

import dace
from dace import nodes as nd
from dace.sdfg.state import ConditionalBlock, LoopRegion

import networkx as nx


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _topo_children(cfg) -> list:
    """Topological sort of the direct children of *cfg*."""
    G = nx.DiGraph()
    for n in cfg.nodes():
        G.add_node(n)
    for e in cfg.edges():
        G.add_edge(e.src, e.dst)
    return list(nx.topological_sort(G))


def _is_write(state: dace.SDFGState, node: nd.AccessNode) -> bool:
    return state.in_degree(node) > 0


def _is_read(state: dace.SDFGState, node: nd.AccessNode) -> bool:
    return state.out_degree(node) > 0


def _collect_always_write_then_read_transients(sdfg: dace.SDFG) -> set[tuple[str, dace.SDFG]]:
    """Return names of transient scalars that are always written before read.

    Any access nodes of these transients are always written first before any
    read happens, in any level of the nestedness.
    """
    pure_reads: set[tuple[str, dace.SDFG]] = set()
    always_wtr: dict[tuple[str, dace.SDFG], int] = dict()
    for node, state in sdfg.all_nodes_recursive():
        if not isinstance(node, nd.AccessNode):
            continue
        if (node.data, state.sdfg) in pure_reads:
            continue
        if state.in_degree(node) == 0:
            pure_reads.add((node.data, state.sdfg))
        else:
            always_wtr[(node.data, state.sdfg)] = always_wtr.get((node.data, state.sdfg), 0) + 1
    for v, g in always_wtr.keys():
        if (v, g) in pure_reads:
            continue
        for e, st in g.all_edges_recursive():
            if st.sdfg != g:
                continue
            if not isinstance(e.data, dace.InterstateEdge) or not e.data.assignments:
                continue
            if v in e.data.read_symbols():
                pure_reads.add((v, g))
    for cb, state in sdfg.all_nodes_recursive():
        if not isinstance(cb, ConditionalBlock):
            continue
        for v in cb.free_symbols:
            pure_reads.add((v, state.sdfg))
    return {var_sdfg for var_sdfg, count in always_wtr.items() if var_sdfg not in pure_reads and count > 1}


def _collect_multi_write_transients(sdfg: dace.SDFG) -> set[str]:
    """Return names of transient scalars written more than once.

    Only includes Scalar descriptors — SSA is incorrect for arrays with
    partial writes (e.g. writing one element at a time), because each SSA
    version gets its own allocation and earlier element-writes are lost.
    """
    write_counts: dict[str, int] = defaultdict(int)
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nd.AccessNode) and _is_write(state, node):
                write_counts[node.data] += 1
    return {
        name
        for name, count in write_counts.items()
        if count > 1
        and name in sdfg.arrays
        and sdfg.arrays[name].transient
        and isinstance(sdfg.arrays[name], dace.data.Scalar)
    }


def _mint_name(sdfg: dace.SDFG, base: str, counter: dict[str, int]) -> str:
    """Create a fresh SSA name like ``base__v2`` and register the descriptor."""
    counter[base] = counter.get(base, 0) + 1
    ver = counter[base]
    new_name = f"{base}__v{ver}"
    while new_name in sdfg.arrays or new_name in sdfg.symbols or new_name in sdfg.constants:
        ver += 1
        counter[base] = ver
        new_name = f"{base}__v{ver}"
    # Clone the descriptor
    orig = sdfg.arrays[base]
    new_desc = copy.deepcopy(orig)
    new_desc.transient = True
    sdfg.add_datadesc(new_name, new_desc)
    return new_name


def _rename_access_node(state: dace.SDFGState, node: nd.AccessNode,
                        old_name: str, new_name: str):
    """Rename a specific AccessNode and all edges in its memlet tree.

    Traces through map entry/exit scopes to rename inner edges and connectors.
    """
    node.data = new_name

    # Collect all edges in the memlet trees rooted at this node.
    edges_to_rename: set = set()

    # Outgoing memlet trees (node is source — reads from this node)
    for e in state.out_edges(node):
        edges_to_rename.add(id(e))
        try:
            mt = state.memlet_tree(e)
            for tree_node in mt.traverse_children(include_self=True):
                edges_to_rename.add(id(tree_node.edge))
        except (ValueError, StopIteration):
            pass

    # Incoming memlet trees (node is destination — writes to this node)
    for e in state.in_edges(node):
        edges_to_rename.add(id(e))
        try:
            mt = state.memlet_tree(e)
            for tree_node in mt.traverse_children(include_self=True):
                edges_to_rename.add(id(tree_node.edge))
        except (ValueError, StopIteration):
            pass

    # Rename map entry/exit connector dicts FIRST (before renaming edge connectors)
    for n in state.nodes():
        if isinstance(n, (nd.MapEntry, nd.MapExit)):
            for prefix in ("IN_", "OUT_"):
                old_conn = prefix + old_name
                new_conn = prefix + new_name
                if old_conn in n.in_connectors:
                    connected = any(
                        id(e) in edges_to_rename
                        for e in state.in_edges(n) if e.dst_conn == old_conn
                    )
                    if connected:
                        n.in_connectors[new_conn] = n.in_connectors.pop(old_conn)
                if old_conn in n.out_connectors:
                    connected = any(
                        id(e) in edges_to_rename
                        for e in state.out_edges(n) if e.src_conn == old_conn
                    )
                    if connected:
                        n.out_connectors[new_conn] = n.out_connectors.pop(old_conn)

    # Rename memlets and edge connector references
    for e in state.edges():
        if id(e) in edges_to_rename and e.data.data == old_name:
            e.data.data = new_name
            if e.src_conn == f"OUT_{old_name}":
                e._src_conn = f"OUT_{new_name}"
            if e.dst_conn == f"IN_{old_name}":
                e._dst_conn = f"IN_{new_name}"


def _rename_in_state(state: dace.SDFGState, old_name: str, new_name: str):
    """Rename ALL references to old_name in a state (used by _revert_versions)."""
    for node in state.nodes():
        if isinstance(node, nd.AccessNode) and node.data == old_name:
            _rename_access_node(state, node, old_name, new_name)


# ---------------------------------------------------------------------------
# core: reaching-definition walk
# ---------------------------------------------------------------------------

class _VersionMap:
    """Maps original variable name → current SSA name."""

    def __init__(self, init: dict[str, str] | None = None):
        self._map: dict[str, str] = dict(init) if init else {}

    def get(self, name: str) -> str:
        return self._map.get(name, name)

    def set(self, name: str, ssa_name: str):
        self._map[name] = ssa_name

    def copy(self) -> "_VersionMap":
        return _VersionMap(self._map)

    def items(self):
        return self._map.items()

    def __repr__(self):
        return f"VersionMap({self._map})"


def _process_state(
    sdfg: dace.SDFG,
    state: dace.SDFGState,
    vmap: _VersionMap,
    targets: set[str],
    counter: dict[str, int],
):
    """Process a single state: rename reads first, then writes.

    Returns set of variables written in this state.
    """
    written_here: set[str] = set()

    # Collect access nodes, classifying each
    reads: list[tuple[nd.AccessNode, str]] = []      # read-only
    writes: list[tuple[nd.AccessNode, str]] = []     # write-only
    readwrites: list[tuple[nd.AccessNode, str]] = [] # both

    for node in state.nodes():
        if isinstance(node, nd.AccessNode) and node.data in targets:
            is_w = _is_write(state, node)
            is_r = _is_read(state, node)
            if is_r and is_w:
                readwrites.append((node, node.data))
            elif is_r:
                reads.append((node, node.data))
            elif is_w:
                writes.append((node, node.data))

    # Step 1: Rename pure reads to current version (BEFORE any writes)
    for node, orig_name in reads:
        cur = vmap.get(orig_name)
        if cur != orig_name:
            _rename_access_node(state, node, orig_name, cur)

    # Step 2: Rename pure writes — mint new version
    for node, orig_name in writes:
        new_name = _mint_name(sdfg, orig_name, counter)
        _rename_access_node(state, node, orig_name, new_name)
        vmap.set(orig_name, new_name)
        written_here.add(orig_name)

    # Step 3: Read-write nodes.
    # A single AccessNode with both in-edges and out-edges means data flows
    # through it: producer → [AN] → consumer. The write creates the value
    # that the read then passes on. So: mint new version for the whole node.
    for node, orig_name in readwrites:
        new_name = _mint_name(sdfg, orig_name, counter)
        _rename_access_node(state, node, orig_name, new_name)
        vmap.set(orig_name, new_name)
        written_here.add(orig_name)

    return written_here


def _first_access_in_body(cfg, varname: str) -> str | None:
    """Check whether the first access to `varname` in cfg's body is a read or write.

    Returns 'R', 'W', 'RW', or None if not accessed.
    Walks topological order, returns at first region that accesses it.
    """
    for child in _topo_children(cfg):
        if isinstance(child, dace.SDFGState):
            for node in child.nodes():
                if isinstance(node, nd.AccessNode) and node.data == varname:
                    r = _is_read(child, node)
                    w = _is_write(child, node)
                    if r and w:
                        return "RW"
                    elif r:
                        return "R"
                    elif w:
                        return "W"
        else:
            # Recurse into sub-regions
            result = _first_access_in_body(child, varname)
            if result is not None:
                return result
    return None


def _rename_single_edge(edge, vmap: _VersionMap, targets: set[str]):
    """Rename variable references in a single interstate edge."""
    from dace.properties import CodeBlock

    # Rename in assignments (values only — keys are symbol names, not data)
    if hasattr(edge, 'assignments') and edge.assignments:
        new_assigns = {}
        for k, v in edge.assignments.items():
            v_str = str(v)
            for orig_name in targets:
                cur = vmap.get(orig_name)
                if cur != orig_name and orig_name in v_str:
                    v_str = re.sub(
                        r'\b' + re.escape(orig_name) + r'\b',
                        cur, v_str
                    )
            new_assigns[k] = v_str
        edge.assignments = new_assigns

    # Rename in conditions
    if hasattr(edge, 'condition'):
        cond_str = edge.condition.as_string
        changed = False
        for orig_name in targets:
            cur = vmap.get(orig_name)
            if cur != orig_name and orig_name in cond_str:
                cond_str = re.sub(
                    r'\b' + re.escape(orig_name) + r'\b',
                    cur, cond_str
                )
                changed = True
        if changed:
            edge.condition = CodeBlock(cond_str)


def _rename_outgoing_edges(cfg, node, vmap: _VersionMap, targets: set[str]):
    """Rename interstate edges leaving `node` using the current vmap."""
    for e in cfg.out_edges(node):
        # DaCe edges unpack as (src, dst, data) or have .data attribute
        try:
            _, _, edge_data = e
        except (TypeError, ValueError):
            edge_data = e.data if hasattr(e, 'data') else e
        _rename_single_edge(edge_data, vmap, targets)


def _rename_cfgnode_expressions(sdfg: dace.SDFG, node, vmap: _VersionMap,
                                targets: set[str]):
    """Rename variable references in LoopRegion init/update/condition."""
    from dace.properties import CodeBlock

    if not isinstance(node, LoopRegion):
        return

    for attr in ('init_statement', 'update_statement', 'loop_condition'):
        val = getattr(node, attr, None)
        if val is None:
            continue
        val_str = val.as_string if hasattr(val, 'as_string') else str(val)
        changed = False
        for orig_name in targets:
            cur = vmap.get(orig_name)
            if cur != orig_name and re.search(r'\b' + re.escape(orig_name) + r'\b', val_str):
                val_str = re.sub(r'\b' + re.escape(orig_name) + r'\b', cur, val_str)
                changed = True
        if changed:
            setattr(node, attr, CodeBlock(val_str))


def _rename_conditional_conditions(sdfg: dace.SDFG, node: ConditionalBlock,
                                   vmap: _VersionMap, targets: set[str]):
    """Rename variable references in ConditionalBlock branch conditions.

    Also registers SSA scalar names as symbols so codegen can find them.
    """
    from dace.properties import CodeBlock

    for i, (cond, region) in enumerate(node.branches):
        if cond is None:
            continue
        cond_str = cond.as_string
        changed = False
        for orig_name in targets:
            cur = vmap.get(orig_name)
            if cur != orig_name and re.search(r'\b' + re.escape(orig_name) + r'\b', cond_str):
                cond_str = re.sub(r'\b' + re.escape(orig_name) + r'\b', cur, cond_str)
                changed = True
        if changed:
            node.branches[i] = (CodeBlock(cond_str), region)


def _process_cfg(
    sdfg: dace.SDFG,
    cfg,
    vmap: _VersionMap,
    targets: set[str],
    counter: dict[str, int],
):
    """Recursively process a control-flow region."""
    children = _topo_children(cfg)

    for child in children:
        if isinstance(child, dace.SDFGState):
            _process_state(sdfg, child, vmap, targets, counter)

        elif isinstance(child, LoopRegion):
            # Rename references in loop init/update/condition
            _rename_cfgnode_expressions(sdfg, child, vmap, targets)

            # BEFORE processing the body, determine first-access type for
            # each target variable (names are still original at this point).
            first_access: dict[str, str | None] = {}
            for orig_name in targets:
                first_access[orig_name] = _first_access_in_body(child, orig_name)

            # Snapshot version map at loop entry
            entry_vmap = vmap.copy()

            # Process loop body (renames AccessNodes to SSA versions)
            _process_cfg(sdfg, child, vmap, targets, counter)

            # Detect loop-carried dependencies:
            # If a variable's version changed in the body AND its first access
            # in the body is a READ (before any write), then we have a
            # loop-carried dependency — we must unify to the entry version.
            for orig_name in list(targets):
                entry_ver = entry_vmap.get(orig_name)
                exit_ver = vmap.get(orig_name)
                if entry_ver == exit_ver:
                    continue  # not written in this loop body

                first = first_access.get(orig_name)
                if first in ("R", "RW"):
                    # Loop-carried: revert all SSA versions created inside
                    # this loop body back to the entry version.
                    _revert_versions(child, orig_name, entry_ver)
                    vmap.set(orig_name, entry_ver)
                # else: first access is W — safe, the loop just overwrites
                # each iteration with no carry-over. Keep SSA versions.

        elif isinstance(child, ConditionalBlock):
            # Rename branch conditions using current vmap
            _rename_conditional_conditions(sdfg, child, vmap, targets)

            # Process each branch independently
            branch_vmaps = []
            for cond, region in child.branches:
                branch_vmap = vmap.copy()
                _process_cfg(sdfg, region, branch_vmap, targets, counter)
                branch_vmaps.append(branch_vmap)

            # Merge: for each variable, check if branches agree
            for orig_name in list(targets):
                versions = set()
                for bvmap in branch_vmaps:
                    versions.add(bvmap.get(orig_name))
                # Also include the pre-conditional version (for branches that
                # don't write the variable at all)
                pre_ver = vmap.get(orig_name)

                if len(versions) == 1:
                    # All branches agree — use that version
                    vmap.set(orig_name, versions.pop())
                else:
                    # Branches disagree — keep pre-conditional version.
                    # This means we need to revert the branch-specific versions.
                    # The reads after the conditional will use the pre-cond version.
                    # This is conservative but safe: it just means we don't SSA
                    # across conditionals that write different versions.
                    for bvmap in branch_vmaps:
                        bver = bvmap.get(orig_name)
                        if bver != pre_ver and bver != orig_name:
                            # Revert this branch's writes back to pre_ver
                            _revert_versions(
                                child, orig_name, pre_ver,
                            )
                    vmap.set(orig_name, pre_ver)

        elif hasattr(child, 'nodes'):
            # Generic sub-region
            _process_cfg(sdfg, child, vmap, targets, counter)

        # Rename outgoing interstate edges from this child using the
        # CURRENT vmap (i.e. the vmap right after processing this child).
        # This is critical: edges must use the version that's live when
        # the edge executes, not the final version after all children.
        _rename_outgoing_edges(cfg, child, vmap, targets)


def _revert_versions(cfg, orig_name: str, target_ver: str):
    """Revert all SSA-renamed references for `orig_name` back to `target_ver`
    within the given CFG region.  Handles AccessNodes, interstate edges,
    ConditionalBlock conditions, and LoopRegion expressions."""
    from dace.properties import CodeBlock

    # Match any SSA version of orig_name: orig_name__vN
    pattern = re.compile(r'^' + re.escape(orig_name) + r'__v\d+$')
    word_pattern = re.compile(r'\b' + re.escape(orig_name) + r'__v\d+\b')

    # Revert AccessNodes
    states = cfg.all_states() if not isinstance(cfg, dace.SDFGState) else [cfg]
    for state in states:
        to_revert = []
        for node in state.nodes():
            if isinstance(node, nd.AccessNode):
                if pattern.match(node.data) or node.data == orig_name:
                    if node.data != target_ver:
                        to_revert.append((node, node.data))
        for node, old in to_revert:
            _rename_access_node(state, node, old, target_ver)

    # Revert ConditionalBlock conditions and LoopRegion expressions
    all_regions = [cfg] if not isinstance(cfg, dace.SDFGState) else []
    if hasattr(cfg, 'all_control_flow_regions'):
        all_regions = list(cfg.all_control_flow_regions())

    for region in all_regions:
        for child in region.nodes():
            if isinstance(child, ConditionalBlock):
                for i, (cond, branch_region) in enumerate(child.branches):
                    if cond is not None:
                        cond_str = cond.as_string
                        new_str = word_pattern.sub(target_ver, cond_str)
                        if new_str != cond_str:
                            child.branches[i] = (CodeBlock(new_str), branch_region)
            elif isinstance(child, LoopRegion):
                for attr in ('init_statement', 'update_statement', 'loop_condition'):
                    val = getattr(child, attr, None)
                    if val is None:
                        continue
                    val_str = val.as_string if hasattr(val, 'as_string') else str(val)
                    new_str = word_pattern.sub(target_ver, val_str)
                    if new_str != val_str:
                        setattr(child, attr, CodeBlock(new_str))

    # Revert interstate edges (in cfg and all nested regions)
    for region in all_regions:
        for _src, _dst, edge in region.edges():
            if hasattr(edge, 'assignments') and edge.assignments:
                new_assigns = {}
                for k, v in edge.assignments.items():
                    new_assigns[k] = word_pattern.sub(target_ver, str(v))
                edge.assignments = new_assigns
            if hasattr(edge, 'condition'):
                cond_str = edge.condition.as_string
                new_str = word_pattern.sub(target_ver, cond_str)
                if new_str != cond_str:
                    edge.condition = CodeBlock(new_str)


# ---------------------------------------------------------------------------
# cleanup: remove unused SSA descriptors
# ---------------------------------------------------------------------------

def _cleanup_unused_descriptors(sdfg: dace.SDFG):
    """Remove SSA descriptors (__v*) that are no longer referenced by any AccessNode.

    Only removes __v* names — never removes original descriptors, since those
    may be referenced by interstate edges, symbols, or other metadata.
    """
    ssa_pattern = re.compile(r'__v\d+$')
    used = set()
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nd.AccessNode):
                used.add(node.data)
    to_remove = [
        name for name in list(sdfg.arrays.keys())
        if ssa_pattern.search(name) and name not in used
    ]
    for name in to_remove:
        del sdfg.arrays[name]
    return len(to_remove)


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




# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def ssa_transform_wtr(sdfg: dace.SDFG):
    """SSA transform for write-then-read variables (always written before read).

    This is a simpler version of SSA that only handles the special case of
    variables that are always written before read It does not do reaching-def
    analysis or handle loops/conditionals — it just renames each write to a
    unique version, and then renames all reads to the last version.

    This is useful as a simpler alternative to full SSA for debugging, and can
    still enable some optimizations.
    """
    targets = _collect_always_write_then_read_transients(sdfg)
    if not targets:
        print("No always-write-then-read transient scalars found.")
        return
    print(f"SSA candidates: {len(targets)} transient scalars always written before read")
    identifiers = _create_identifier_dict(sdfg)
    counter: dict[str, int] = {}
    for name, sdfg in identifiers:
        counter[name] = 0
    for node, state in sdfg.all_nodes_recursive():
        if not isinstance(node, nd.AccessNode):
            continue
        if (node.data, state.sdfg) not in targets:
            continue
        orig_name = node.data
        new_name = _mint_name(state.sdfg, orig_name, counter)
        _rename_access_node(state, node, orig_name, new_name)
        for cb in state.sdfg.all_control_flow_regions():
            if not isinstance(cb, ConditionalBlock) or cb.sdfg is not state.sdfg:
                continue
            cb.replace_dict({orig_name: new_name})
        print(f"Renamed {orig_name} to {new_name} in state {state.label}")


def ssa_transform(sdfg: dace.SDFG, only: set[str] | None = None) -> dict[str, list[str]]:
    """Apply SSA renaming to all multi-write transients.

    Args:
        only: If given, restrict SSA to only these variable names (for debugging).

    Returns a dict mapping original name → list of SSA versions created.
    """
    targets = _collect_multi_write_transients(sdfg)
    if only:
        targets = targets & only
    if not targets:
        print("No multi-write transients found.")
        return {}

    print(f"SSA candidates: {len(targets)} transient variables with >1 write")

    counter: dict[str, int] = {}
    vmap = _VersionMap()

    _process_cfg(sdfg, sdfg, vmap, targets, counter)

    # Build result map
    result: dict[str, list[str]] = {}
    pattern_cache = {}
    for base in targets:
        if base not in pattern_cache:
            pattern_cache[base] = re.compile(
                r'^' + re.escape(base) + r'__v\d+$'
            )
        versions = sorted(
            name for name in sdfg.arrays
            if pattern_cache[base].match(name)
        )
        if versions:
            result[base] = versions

    removed = _cleanup_unused_descriptors(sdfg)

    # Stats
    total_versions = sum(len(v) for v in result.values())
    print(f"Created {total_versions} SSA versions for {len(result)} variables")
    print(f"Removed {removed} unused descriptors")

    # Verify: no multi-write transients should remain (except loop-carried)
    remaining = _collect_multi_write_transients(sdfg)
    if remaining:
        print(f"Note: {len(remaining)} variables still have >1 write (loop-carried or conditional merges)")
        for name in sorted(remaining):
            wc = sum(
                1 for s in sdfg.all_states()
                for n in s.nodes()
                if isinstance(n, nd.AccessNode) and n.data == name and _is_write(s, n)
            )
            print(f"  {name}: {wc} writes")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SSA transform for DaCe SDFGs")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Output SDFG file (default: <input>_ssa.sdfgz)")
    parser.add_argument("--only", type=str, default=None,
                        help="Comma-separated list of variable names to SSA (for debugging)")
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.input)
    only_set = set(args.only.split(",")) if args.only else None
    result = ssa_transform(sdfg, only=only_set)

    out_path = args.output or args.input.replace(".sdfgz", "_ssa.sdfgz")
    sdfg.save(out_path)
    print(f"Saved to {out_path}")
