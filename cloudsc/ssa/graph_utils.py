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

    repl = {}
    for name in written:
        new_name = name + suffix
        # Ensure uniqueness — find a free name if there's a collision
        counter = 0
        while new_name in sdfg.arrays or new_name in sdfg.symbols:
            counter += 1
            new_name = f"{name}{suffix}_ls{counter}"
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
