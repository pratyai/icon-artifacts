"""graph_utils.py — Low-level SDFG graph manipulation utilities.

Provides functions for surgical renaming of data, connectors, and symbols
within SDFG states and regions.
"""

from __future__ import annotations

import re
from typing import Set, Dict, Any, List, Optional, Union

import dace
from dace import nodes as nd, symbolic
from dace.sdfg.state import ControlFlowRegion, SDFGState, LoopRegion, ConditionalBlock, ControlFlowBlock
import networkx as nx


def collect_memlet_tree_edges(state: SDFGState, node: nd.AccessNode) -> Set[dace.sdfg.graph.MultiConnectorEdge]:
    """Return all edges in the memlet trees connected to the given AccessNode."""
    res = set()
    # TODO: If memlet_tree fails due to inconsistent connectivity, we might
    # need a custom traversal. For now, we prefer to crash rather than
    # perform an incomplete surgical rename.
    for e in state.out_edges(node):
        res.add(e)
        mt = state.memlet_tree(e)
        for tree_node in mt.traverse_children(include_self=True):
            res.add(tree_node.edge)
    for e in state.in_edges(node):
        res.add(e)
        mt = state.memlet_tree(e)
        for tree_node in mt.traverse_children(include_self=True):
            res.add(tree_node.edge)
    return res


def is_write(state: SDFGState, node: nd.AccessNode) -> bool:
    """Check if the AccessNode is a write definition in the given state."""
    return state.in_degree(node) > 0


def is_read(state: SDFGState, node: nd.AccessNode) -> bool:
    """Check if the AccessNode is a read reference in the given state."""
    return state.out_degree(node) > 0


def rename_access_node_and_tree(state: SDFGState, node: nd.AccessNode, old_name: str, new_name: str):
    """Rename a specific AccessNode and all edges in its memlet tree.
    
    Also renames IN_/OUT_ connectors on scope nodes and NestedSDFG nodes if they match.
    """
    node.data = new_name
    edges_to_rename = collect_memlet_tree_edges(state, node)

    rename_map_connectors(state, old_name, new_name, edges_to_rename)
    rename_nested_sdfg_connectors(state, old_name, new_name, edges_to_rename)
    rename_memlet_data(state, old_name, new_name, edges_to_rename)


def rename_map_connectors(state: SDFGState, old_name: str, new_name: str, 
                          edges_to_rename: Set[dace.sdfg.graph.MultiConnectorEdge]):
    """Rename IN_/OUT_ connectors on Map nodes if they match the renamed data."""
    for n in state.nodes():
        if isinstance(n, (nd.MapEntry, nd.MapExit)):
            for prefix in ("IN_", "OUT_"):
                old_conn = prefix + old_name
                new_conn = prefix + new_name
                if old_conn in n.in_connectors:
                    if any(e in edges_to_rename for e in state.in_edges(n) if e.dst_conn == old_conn):
                        n.add_in_connector(new_conn)
                        for e in state.in_edges(n):
                            if e.dst_conn == old_conn and e in edges_to_rename:
                                e._dst_conn = new_conn
                        n.remove_in_connector(old_conn)
                if old_conn in n.out_connectors:
                    if any(e in edges_to_rename for e in state.out_edges(n) if e.src_conn == old_conn):
                        n.add_out_connector(new_conn)
                        for e in state.out_edges(n):
                            if e.src_conn == old_conn and e in edges_to_rename:
                                e._src_conn = new_conn
                        n.remove_out_connector(old_conn)


def rename_nested_sdfg_connectors(state: SDFGState, old_name: str, new_name: str, 
                                  edges_to_rename: Set[dace.sdfg.graph.MultiConnectorEdge]):
    """Rename connectors on NestedSDFG nodes and update internal SDFG definitions/metadata."""
    for n in state.nodes():
        if isinstance(n, nd.NestedSDFG):
            # 1. Handle Input Connectors
            if old_name in n.in_connectors:
                if any(e in edges_to_rename for e in state.in_edges(n) if e.dst_conn == old_name):
                    n.add_in_connector(new_name)
                    for e in state.in_edges(n):
                        if e.dst_conn == old_name and e in edges_to_rename:
                            e._dst_conn = new_name
                    n.remove_in_connector(old_name)
                    
                    # If the internal SDFG has this as a non-transient, rename it there too.
                    # We do this surgically to avoid aggressive replacement.
                    if old_name in n.sdfg.arrays and not n.sdfg.arrays[old_name].transient:
                        # Rename the descriptor
                        desc = n.sdfg.arrays.pop(old_name)
                        n.sdfg.add_datadesc(new_name, desc)

                        # Surgically update all references inside the nested SDFG
                        # Update AccessNodes
                        for sd_state in n.sdfg.states():
                            for sd_node in sd_state.nodes():
                                if isinstance(sd_node, nd.AccessNode) and sd_node.data == old_name:
                                    sd_node.data = new_name
                            # Update memlet data references in this state's edges
                            for sd_edge in sd_state.edges():
                                if sd_edge.data is not None and sd_edge.data.data == old_name:
                                    sd_edge.data.data = new_name

                        # Update metadata
                        update_metadata(n.sdfg, {old_name: new_name}, recursive=True)
                        for edge, _ in n.sdfg.all_edges_recursive():
                            if isinstance(edge.data, dace.InterstateEdge):
                                update_interstate_edge(edge.data, {old_name: new_name})

            # 2. Handle Output Connectors
            if old_name in n.out_connectors:
                if any(e in edges_to_rename for e in state.out_edges(n) if e.src_conn == old_name):
                    n.add_out_connector(new_name)
                    for e in state.out_edges(n):
                        if e.src_conn == old_name and e in edges_to_rename:
                            e._src_conn = new_name
                    n.remove_out_connector(old_name)
                    
                    if old_name in n.sdfg.arrays and not n.sdfg.arrays[old_name].transient:
                        desc = n.sdfg.arrays.pop(old_name)
                        n.sdfg.add_datadesc(new_name, desc)
                        for sd_state in n.sdfg.states():
                            for sd_node in sd_state.nodes():
                                if isinstance(sd_node, nd.AccessNode) and sd_node.data == old_name:
                                    sd_node.data = new_name
                        update_metadata(n.sdfg, {old_name: new_name}, recursive=True)
                        for edge, _ in n.sdfg.all_edges_recursive():
                            if isinstance(edge.data, dace.InterstateEdge):
                                update_interstate_edge(edge.data, {old_name: new_name})


def rename_memlet_data(state: SDFGState, old_name: str, new_name: str, 
                        edges_to_rename: Set[dace.sdfg.graph.MultiConnectorEdge]):
    """Rename the data reference in memlets belonging to the target tree."""
    for e in state.edges():
        if e in edges_to_rename and e.data.data == old_name:
            e.data.data = new_name


def update_node_content(node: nd.Node, repl: Dict[str, str]):
    """Update symbol references in node properties (Tasklets, NestedSDFGs, Maps, etc.)."""
    from dace.sdfg.replace import replace_properties_dict
    
    if not repl:
        return

    # Handle NestedSDFG symbol mapping specifically (can be expressions)
    if isinstance(node, nd.NestedSDFG):
        new_mapping = {}
        for sym, expr in node.symbol_mapping.items():
            if isinstance(expr, str):
                new_expr = expr
                for orig, new in repl.items():
                    new_expr = re.sub(r'\b' + re.escape(orig) + r'\b', new, new_expr)
                new_mapping[sym] = new_expr
            elif isinstance(expr, symbolic.sympy.Basic):  # sympy expression
                subs_map = {symbolic.pystr_to_symbolic(orig): symbolic.pystr_to_symbolic(new) 
                            for orig, new in repl.items()}
                new_mapping[sym] = expr.subs(subs_map)
            else:
                new_mapping[sym] = expr
        node.symbol_mapping = new_mapping
    
    # Generic property replacement (Tasklet code, Map ranges, etc.)
    replace_properties_dict(node, repl)


def update_interstate_edge(edge: dace.InterstateEdge, repl: Dict[str, str]):
    """Update symbol references in an InterstateEdge's conditions and assignments."""
    from dace.properties import CodeBlock
    
    for orig, replacement in repl.items():
        pattern = r'\b' + re.escape(orig) + r'\b'
        
        # Conditions
        if edge.condition is not None:
            c_str = edge.condition.as_string
            if re.search(pattern, c_str):
                new_c = re.sub(pattern, replacement, c_str)
                edge.condition = CodeBlock(new_c)
        
        # Assignments
        if edge.assignments:
            new_assigns = {}
            for k, v in edge.assignments.items():
                v_str = str(v)
                # Skip reindexing if this is a prologue/epilogue assignment
                # (e.g. s_ext = s should not become s_ext[0] = s_ext[0])
                if (k == replacement.split('[')[0] and v_str == orig) or \
                   (k == orig and v_str == replacement):
                    new_assigns[k] = v
                    continue

                # Rename the value (RHS)
                if re.search(pattern, v_str):
                    v_str = re.sub(pattern, replacement, v_str)
                
                # Rename the key (LHS) if it matches the original data name.
                # Keys must be valid symbol names — never rename to an indexed
                # expression (e.g. "arr[i]") as that's invalid for assignment LHS.
                new_k = k
                if k == orig and '[' not in replacement:
                    new_k = replacement
                
                new_assigns[new_k] = v_str
            edge.assignments = new_assigns


def update_conditional_metadata(block: ConditionalBlock, repl: Dict[str, str]):
    """Update branch conditions in a ConditionalBlock."""
    from dace.properties import CodeBlock
    for orig, replacement in repl.items():
        pattern = r'\b' + re.escape(orig) + r'\b'
        for i, (cond, branch) in enumerate(block.branches):
            if cond is not None:
                c_str = cond.as_string
                if re.search(pattern, c_str):
                    new_c = re.sub(pattern, replacement, c_str)
                    block.branches[i] = (CodeBlock(new_c), branch)


def update_loop_metadata(block: LoopRegion, repl: Dict[str, str]):
    """Update init, condition, and update statements in a LoopRegion."""
    from dace.properties import CodeBlock
    for orig, replacement in repl.items():
        pattern = r'\b' + re.escape(orig) + r'\b'
        # init_statement
        if block.init_statement:
            v_str = block.init_statement.as_string
            if re.search(pattern, v_str):
                block.init_statement = CodeBlock(re.sub(pattern, replacement, v_str))
        # loop_condition
        if block.loop_condition:
            v_str = block.loop_condition.as_string
            if re.search(pattern, v_str):
                block.loop_condition = CodeBlock(re.sub(pattern, replacement, v_str))
        # update_statement
        if block.update_statement:
            v_str = block.update_statement.as_string
            if re.search(pattern, v_str):
                block.update_statement = CodeBlock(re.sub(pattern, replacement, v_str))


def update_block_metadata(block: ControlFlowBlock, repl: Dict[str, str]):
    """Update metadata for a single ControlFlowBlock."""
    from dace.sdfg.replace import replace_properties_dict

    if isinstance(block, ConditionalBlock):
        update_conditional_metadata(block, repl)
    elif isinstance(block, LoopRegion):
        update_loop_metadata(block, repl)
    
    # Still call this for any other properties
    replace_properties_dict(block, repl)


def update_metadata(block: Union[ControlFlowBlock, ControlFlowRegion, dace.SDFG], 
                    repl: Dict[str, str], recursive: bool = False):
    """Update symbol references in LoopRegion or ConditionalBlock metadata."""
    if isinstance(block, ControlFlowBlock):
        update_block_metadata(block, repl)

    if recursive and isinstance(block, (ControlFlowRegion, dace.SDFG)):
        for b in block.all_control_flow_blocks():
            update_block_metadata(b, repl)


def get_enclosing_loop(block: Union[ControlFlowBlock, ControlFlowRegion, SDFGState, dace.SDFG]) -> Optional[LoopRegion]:
    """Find the immediate enclosing LoopRegion for a given block, region, state or SDFG."""
    curr: Optional[Any] = block
    while curr is not None:
        p = None
        # For an SDFG (potentially nested), the parent is the SDFGState it belongs to.
        # We check this first because SDFG is also a ControlFlowRegion.
        if isinstance(curr, dace.SDFG):
            p = curr.parent
        # SDFGStates, LoopRegions, and ConditionalBlocks all use parent_graph 
        # to point to their immediate container in the control flow.
        elif isinstance(curr, (SDFGState, ControlFlowBlock, ControlFlowRegion)):
            p = curr.parent_graph
        
        if isinstance(p, LoopRegion):
            return p
        curr = p
    return None


def get_direct_child(parent: Union[ControlFlowRegion, dace.SDFG], descendant: Any) -> Optional[ControlFlowBlock]:
    """Find the ancestor of `descendant` that is a direct child of `parent`."""
    curr = descendant
    while curr is not None:
        p = None
        if isinstance(curr, dace.SDFG):
            p = curr.parent
        elif isinstance(curr, (SDFGState, ControlFlowBlock, ControlFlowRegion)):
            p = curr.parent_graph
        
        if p == parent:
            # If curr is an SDFG, its representative in parent is the NestedSDFG node
            if isinstance(curr, dace.SDFG):
                return curr.parent_nsdfg_node
            return curr
        curr = p
    return None


def topological_sort(graph: Union[ControlFlowRegion, SDFGState, dace.SDFG, ConditionalBlock]) -> List[Any]:
    """Safe topological sort of nodes in a state or blocks in a region."""
    if isinstance(graph, SDFGState):
        # States should always be DAGs. If there's a cycle, we prefer to crash.
        return list(nx.topological_sort(graph._nx))
    elif isinstance(graph, (ControlFlowRegion, dace.SDFG)):
        # For regions, we need to build the graph from nodes/edges
        G = nx.DiGraph()
        G.add_nodes_from(graph.nodes())
        G.add_edges_from((e.src, e.dst) for e in graph.edges())
        try:
            return list(nx.topological_sort(G))
        except nx.NetworkXUnfeasible:
            # TODO: Cycles are expected in ControlFlowRegions (loops), but
            # returning an arbitrary order might be dangerous for some passes.
            return list(graph.nodes())
    elif isinstance(graph, ConditionalBlock):
        # For a conditional block, return its branch regions (parallel alternatives)
        return [branch for _, branch in graph.branches]
    
    raise TypeError(f"Cannot perform topological sort on unknown type: {type(graph)}")


def collect_all_loops(sdfg: dace.SDFG) -> List[LoopRegion]:
    """Return all LoopRegions in the SDFG (including nested ones)."""
    return [child for cfg in sdfg.all_control_flow_regions()
            for child in cfg.nodes() if isinstance(child, LoopRegion)]


def loop_interior(loop: LoopRegion):
    """Return (states, blocks, edges) sets for everything inside the loop."""
    states = set(loop.all_states())
    blocks = set(loop.all_control_flow_blocks())
    edges = {e.data for e, _ in loop.all_edges_recursive()
             if isinstance(e.data, dace.InterstateEdge)}
    return states, blocks, edges


def written_transient_scalars(sdfg: dace.SDFG, states) -> Set[str]:
    """Return transient scalar names written (in_degree > 0) in the given states.

    `states` can be any iterable of SDFGState (e.g. from loop.all_states()
    or a list of copied blocks' states).
    """
    from dace import data as dt

    written = set()
    for state in states:
        for node in state.nodes():
            if isinstance(node, nd.AccessNode) and is_write(state, node):
                name = node.data
                if (name in sdfg.arrays and
                    isinstance(sdfg.arrays[name], dt.Scalar) and
                    sdfg.arrays[name].transient):
                    written.add(name)
    return written


def writeonly_transient_scalars(sdfg: dace.SDFG, states) -> Set[str]:
    """Return transient scalars that are written but NOT read in the given states.

    These are safe to rename per-copy during unrolling because they have no
    inter-iteration data flow (no read depends on a previous iteration's write).
    """
    from dace import data as dt

    written = set()
    read = set()
    for state in states:
        for node in state.nodes():
            if not isinstance(node, nd.AccessNode):
                continue
            name = node.data
            if not (name in sdfg.arrays and
                    isinstance(sdfg.arrays[name], dt.Scalar) and
                    sdfg.arrays[name].transient):
                continue
            if is_write(state, node):
                written.add(name)
            if is_read(state, node):
                read.add(name)
    return written - read


def loop_carried_scalars(sdfg: dace.SDFG, loop) -> Set[str]:
    """Return transient scalars that are loop-carried in `loop`.

    A scalar is loop-carried if, walking the loop body in topological order,
    we encounter a read before any write. This means the first read sees a
    value from a previous iteration.
    """
    from dace import data as dt

    # Collect per-state read/write info for transient scalars
    candidates = set()
    state_reads: Dict[SDFGState, Set[str]] = {}
    state_writes: Dict[SDFGState, Set[str]] = {}
    for state in loop.all_states():
        reads: Set[str] = set()
        writes: Set[str] = set()
        for node in state.nodes():
            if not isinstance(node, nd.AccessNode):
                continue
            name = node.data
            if name not in sdfg.arrays:
                continue
            desc = sdfg.arrays[name]
            if not isinstance(desc, dt.Scalar) or not desc.transient:
                continue
            if is_read(state, node):
                reads.add(name)
            if is_write(state, node):
                writes.add(name)
            candidates.add(name)
        state_reads[state] = reads
        state_writes[state] = writes

    if not candidates:
        return set()

    def _is_read_before_write(name: str, region) -> bool:
        """Return True if `name` is read before written in `region`'s topo order.

        Recurses into compound blocks (ConditionalBlock, ControlFlowRegion)
        to determine the actual ordering within them.
        """
        try:
            topo = topological_sort(region)
        except Exception:
            topo = list(region.nodes())

        for block in topo:
            block_states = [block] if isinstance(block, SDFGState) else list(block.all_states())
            has_read = any(name in state_reads.get(s, set()) for s in block_states)
            has_write = any(name in state_writes.get(s, set()) for s in block_states)

            if not has_read and not has_write:
                continue
            if has_read and not has_write:
                return True  # read-only at this point, no prior write
            if has_write and not has_read:
                return False  # written before any read
            # Both read and write in same block — recurse
            if isinstance(block, SDFGState):
                # Check if any AccessNode for this scalar is read-only
                # (no incoming write edge). If so, that read uses the old value
                # from a prior state → read-before-write.
                # If all AccessNodes have incoming writes (pipeline pattern:
                # write → node → read), it's write-before-read.
                has_readonly_node = False
                for node in block.nodes():
                    if isinstance(node, nd.AccessNode) and node.data == name:
                        if is_read(block, node) and not is_write(block, node):
                            has_readonly_node = True
                            break
                return has_readonly_node
            if isinstance(block, LoopRegion):
                # A child LoopRegion that reads a scalar does so across its
                # own iterations — the first iteration reads the value from
                # the parent's previous iteration. Don't recurse into loops;
                # conservatively treat R+W in a child loop as carried.
                return True
            if isinstance(block, ConditionalBlock):
                # Check each branch; if ANY branch reads before writing, it's carried
                for _, branch in block.branches:
                    if _is_read_before_write(name, branch):
                        return True
                return False
            if isinstance(block, ControlFlowRegion):
                return _is_read_before_write(name, block)
            # Unknown block type — conservative
            return True
        return False  # no access found

    carried = set()
    for name in candidates:
        if _is_read_before_write(name, loop):
            carried.add(name)

    return carried


def all_nodes_in_block(block):
    """Yield (node, state) for all dataflow nodes in a block.

    Works for SDFGState (yields nodes directly), ControlFlowRegion
    (recurses into all_states), and ConditionalBlock (recurses into branches).
    """
    if isinstance(block, SDFGState):
        for node in block.nodes():
            yield node, block
    elif isinstance(block, (ControlFlowRegion, ConditionalBlock)):
        for state in block.all_states():
            for node in state.nodes():
                yield node, state


def rename_local_scalars(sdfg: dace.SDFG, blocks, suffix: str,
                         only: Set[str] | None = None) -> Dict[str, str]:
    """Rename transient scalars written in `blocks` so each copy is unique.

    Creates new descriptors named ``<original><suffix>`` and applies
    ``replace_dict`` within the given blocks only.

    If `only` is provided, only scalars in that set are renamed.

    Returns the replacement dict (old_name -> new_name), empty if nothing renamed.
    """
    import copy

    all_states = []
    for block in blocks:
        if isinstance(block, SDFGState):
            all_states.append(block)
        elif isinstance(block, (ControlFlowRegion, ConditionalBlock)):
            all_states.extend(block.all_states())
    written = written_transient_scalars(sdfg, all_states)
    if only is not None:
        written &= only

    if not written:
        return {}

    taken = all_identifiers(sdfg)
    repl = {}
    for name in written:
        new_name = fresh_name(sdfg, name, suffix, _taken=taken)
        taken.add(new_name)
        new_desc = copy.deepcopy(sdfg.arrays[name])
        new_desc.transient = True
        sdfg.add_datadesc(new_name, new_desc)
        repl[name] = new_name

    if not repl:
        return {}

    for block in blocks:
        block.replace_dict(repl)

    return repl


def lift_data_refs_in_conditions(sdfg: dace.SDFG):
    """Replace data descriptor references in ConditionalBlock conditions with symbols.

    For each scalar data reference found in a branch condition, creates a new
    symbol and assigns the data value to it on the incoming interstate edge(s).
    This ensures conditions only reference symbols, so later passes (e.g. scalar
    expansion) that rewrite data names don't produce invalid condition expressions.
    """
    from dace.properties import CodeBlock

    for cfg in sdfg.all_control_flow_regions():
        for block in cfg.nodes():
            if not isinstance(block, ConditionalBlock):
                continue

            # Collect data references from all branch conditions
            owning_sdfg = cfg.sdfg
            data_refs = set()
            for cond, _ in block.branches:
                if cond is None:
                    continue
                cond_str = cond.as_string
                for name, desc in owning_sdfg.arrays.items():
                    if isinstance(desc, dace.data.Scalar) and \
                       re.search(r'\b' + re.escape(name) + r'\b', cond_str):
                        data_refs.add(name)

            if not data_refs:
                continue

            # Ensure there is at least one incoming edge to carry assignments
            in_edges = list(cfg.in_edges(block))
            if not in_edges:
                init_state = cfg.add_state(f"__init_cond_{block.label}")
                cfg.add_edge(init_state, block, dace.InterstateEdge())
                in_edges = list(cfg.in_edges(block))

            # For each data reference, create a symbol and assign on incoming edges
            repl = {}
            for name in data_refs:
                sym_name = f"__{name}_cond"
                counter = 0
                candidate = sym_name
                while candidate in owning_sdfg.symbols or candidate in owning_sdfg.arrays:
                    counter += 1
                    candidate = f"{sym_name}_{counter}"
                sym_name = candidate

                owning_sdfg.add_symbol(sym_name, owning_sdfg.arrays[name].dtype)
                for edge in in_edges:
                    edge.data.assignments[sym_name] = name
                repl[name] = sym_name

            # Replace data references with symbols in conditions
            for i, (cond, branch) in enumerate(block.branches):
                if cond is None:
                    continue
                cond_str = cond.as_string
                for orig, replacement in repl.items():
                    cond_str = re.sub(r'\b' + re.escape(orig) + r'\b', replacement, cond_str)
                block.branches[i] = (CodeBlock(cond_str), branch)


def condition_free_symbols(cond_code) -> Set[str]:
    """Extract free symbols from a ConditionalBlock condition CodeBlock."""
    expr = symbolic.pystr_to_symbolic(cond_code.as_string)
    return {str(s) for s in expr.free_symbols}


def loop_private_transients(sdfg: dace.SDFG, loop: LoopRegion) -> Set[str]:
    """Return transient names that are written in ``loop`` but NOT accessed outside it."""
    _, write_set = loop.read_and_write_sets()
    loop_states = set(loop.all_states())

    outside_accessed = set()
    for state in sdfg.states():
        if state in loop_states:
            continue
        for dn in state.data_nodes():
            if dn.data in write_set:
                outside_accessed.add(dn.data)

    return {w for w in write_set
            if w in sdfg.arrays and sdfg.arrays[w].transient and w not in outside_accessed}


def interstate_assigned_symbols(region: ControlFlowRegion) -> Set[str]:
    """Return all symbols assigned by interstate edges in ``region``."""
    assigned = set()
    for e in region.edges():
        assigned.update(e.data.assignments.keys())
    return assigned


def scalarize_small_arrays(sdfg: dace.SDFG, max_elements: int = 32) -> int:
    """Replace small constant-indexed transient arrays with individual scalars.

    For each transient array with constant literal shape and all constant-literal
    indexed accesses, replace ``arr[i]`` with ``arr_i`` scalars.

    Returns count of arrays scalarized.
    """
    from dace import data as dt

    scalarized = 0

    for sd in sdfg.all_sdfgs_recursive():
        candidates = []
        for name, desc in list(sd.arrays.items()):
            if not desc.transient:
                continue
            if isinstance(desc, dt.Scalar):
                continue
            # Must have small constant shape
            try:
                shape = tuple(int(s) for s in desc.shape)
            except (TypeError, ValueError):
                continue
            total = 1
            for s in shape:
                total *= s
            if total > max_elements or total == 0:
                continue
            candidates.append((name, desc, shape, total))

        for name, desc, shape, total in candidates:
            # Verify ALL accesses use constant literal indices
            all_constant = True
            for state in sd.states():
                for node in state.data_nodes():
                    if node.data != name:
                        continue
                    for e in state.all_edges(node):
                        if e.data.data != name:
                            continue
                        # Check the subset that indexes into this array
                        subset = e.data.dst_subset if e in state.in_edges(node) else e.data.src_subset
                        if subset is None:
                            # Full-range access like [0:5] — check if it's a full copy
                            all_constant = False
                            break
                        for rng in subset:
                            # Each range element is (start, end, step)
                            start, end, step = rng
                            try:
                                s, e_val = int(start), int(end)
                                if s != e_val:
                                    # Range access, not point access
                                    all_constant = False
                                    break
                            except (TypeError, ValueError):
                                all_constant = False
                                break
                        if not all_constant:
                            break
                    if not all_constant:
                        break
                if not all_constant:
                    break

            if not all_constant:
                continue

            # Create scalar replacements
            taken = set(sd.arrays) | set(sd.symbols)
            scalar_names = {}  # flat_index -> scalar_name
            import numpy as np
            for flat_idx in range(total):
                multi_idx = np.unravel_index(flat_idx, shape)
                suffix = "_" + "_".join(str(i) for i in multi_idx)
                sname = name + suffix
                i = 1
                while sname in taken:
                    sname = f"{name}{suffix}_{i}"
                    i += 1
                taken.add(sname)
                scalar_names[multi_idx] = sname
                sd.add_scalar(sname, desc.dtype, transient=True, storage=desc.storage)

            # Replace all accesses
            for state in sd.states():
                for node in list(state.data_nodes()):
                    if node.data != name:
                        continue
                    # Determine the constant index from any edge
                    for e in list(state.all_edges(node)):
                        if e.data.data != name:
                            continue
                        subset = e.data.dst_subset if e in state.in_edges(node) else e.data.src_subset
                        if subset is None:
                            continue
                        idx = tuple(int(r[0]) for r in subset)
                        sname = scalar_names[idx]
                        # Update the access node
                        node.data = sname
                        # Update the memlet
                        e.data.data = sname
                        e.data.subset = dace.subsets.Range([(0, 0, 1)])
                        if e in state.in_edges(node):
                            e.data.dst_subset = dace.subsets.Range([(0, 0, 1)])
                            if e.data.other_subset is not None:
                                pass  # keep other_subset as-is
                        else:
                            e.data.src_subset = dace.subsets.Range([(0, 0, 1)])

            # Remove original array
            sd.remove_data(name)
            scalarized += 1

    if scalarized:
        print(f"Scalarized {scalarized} small arrays into registers")
    return scalarized


def all_identifiers(sdfg: dace.SDFG) -> Set[str]:
    """Return every name that is already taken in the SDFG (arrays, symbols, constants)."""
    return set(sdfg.arrays) | set(sdfg.symbols) | set(sdfg.constants)


def fresh_name(sdfg: dace.SDFG, base: str, suffix: str = "",
               _taken: Set[str] | None = None) -> str:
    """Mint a name that doesn't collide with any existing SDFG identifier.

    Tries ``base + suffix`` first, then ``base + suffix + _1``, ``_2``, etc.
    Pass ``_taken`` to avoid repeated ``all_identifiers()`` calls in a loop.
    """
    taken = _taken if _taken is not None else all_identifiers(sdfg)
    candidate = base + suffix
    if candidate not in taken:
        return candidate
    i = 1
    while True:
        candidate = f"{base}{suffix}_{i}"
        if candidate not in taken:
            return candidate
        i += 1
