import dace
from typing import Set, Optional
import copy


def _get_missing_symbols(nsdfg_node: dace.nodes.NestedSDFG) -> Set[str]:
    """Identify symbols used inside a NestedSDFG but not mapped in the parent."""
    nsdfg = nsdfg_node.sdfg
    connectors = nsdfg_node.in_connectors.keys() | nsdfg_node.out_connectors.keys()
    
    # Get symbols actually used in the nested SDFG that are not provided via connectors
    used_symbols = set(k for k in nsdfg.used_symbols(all_symbols=False) if k not in connectors)
    
    # Return symbols that are missing from the mapping
    return {s for s in used_symbols if s not in nsdfg_node.symbol_mapping}


def add_missing_symbols_to_nsdfgs(sdfg: dace.SDFG):
    """Recursively add missing symbol mappings to all NestedSDFGs in the graph."""
    nsdfgs = set()
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, dace.nodes.NestedSDFG):
                nsdfgs.add(node.sdfg)
                missing_symbols = _get_missing_symbols(node)
                for ms in missing_symbols:
                    node.symbol_mapping[ms] = ms

    for inner_sdfg in nsdfgs:
        add_missing_symbols_to_nsdfgs(inner_sdfg)


def _insert_missing_data_through_parent_scopes(
    missing_data: Set[str],
    nsdfg_node: dace.nodes.NestedSDFG,
    parent_state: dace.SDFGState,
    parent_sdfg: dace.SDFG,
    descs: Optional[Set[dace.data.Data]] = None,
):
    """Surgically insert data connections through parent scopes into a NestedSDFG."""
    descs = descs or ([None] * len(missing_data))
    
    for data_access, desc in zip(missing_data, descs):
        datadesc = desc or parent_sdfg.arrays[data_access]
        inner_sdfg: dace.SDFG = nsdfg_node.sdfg

        # Skip if already connected to avoid redundant edges
        if (data_access in nsdfg_node.in_connectors and 
            len(list(parent_state.in_edges_by_connector(nsdfg_node, data_access))) > 0):
            continue

        # Clean up namespace: if it's currently a symbol but we need it as data, remove symbol
        if data_access in inner_sdfg.symbols:
            inner_sdfg.remove_symbol(data_access)
            
        # Ensure descriptor exists in nested SDFGs
        if data_access not in inner_sdfg.arrays:
            copydesc = copy.deepcopy(datadesc)
            copydesc.transient = False
            inner_sdfg.add_datadesc(data_access, copydesc)
            
        if data_access not in nsdfg_node.sdfg.arrays:
            copydesc = copy.deepcopy(datadesc)
            copydesc.transient = False
            nsdfg_node.sdfg.add_datadesc(data_access, copydesc)

        # Trace scopes up to find the flow path (e.g. AccessNode -> Map -> NSDFG)
        parent_scopes = []
        cur_node = nsdfg_node
        scope_dict = parent_state.scope_dict()
        while scope_dict[cur_node] is not None:
            parent_scopes.append(scope_dict[cur_node])
            cur_node = scope_dict[cur_node]

        # Start wiring from a new access node at the top-most scope
        src = parent_state.add_access(data_access)
        for scope in reversed(parent_scopes):
            dst = scope
            parent_state.add_edge(
                src, None if isinstance(src, dace.nodes.AccessNode) else f"OUT_{data_access}",
                dst, f"IN_{data_access}",
                dace.memlet.Memlet.from_array(data_access, datadesc),
            )
            if not isinstance(src, dace.nodes.AccessNode):
                src.add_out_connector(f"OUT_{data_access}")
            dst.add_in_connector(f"IN_{data_access}")
            src = scope
            
        # Final link to the NestedSDFG itself
        parent_state.add_edge(
            src, None if isinstance(src, dace.nodes.AccessNode) else f"OUT_{data_access}",
            nsdfg_node, data_access,
            dace.memlet.Memlet.from_array(data_access, datadesc),
        )
        if not isinstance(src, dace.nodes.AccessNode):
            src.add_out_connector(f"OUT_{data_access}")
        nsdfg_node.add_in_connector(data_access)


def add_missing_data_and_symbols(root: dace.SDFG, parent_graph: dace.SDFGState, 
                                 parent_sdfg: dace.SDFG, sdfg: dace.SDFG):
    """Identify and propagate missing symbols and data containers to a NestedSDFG."""
    parent_nsdfg_node = sdfg.parent_nsdfg_node
    missing_symbols = _get_missing_symbols(parent_nsdfg_node)
    
    # Distinguish between symbols that are actually data containers (arrays/scalars)
    missing_data = {s for s in missing_symbols if s in root.arrays or s in parent_sdfg.arrays or s in sdfg.arrays}

    if missing_data:
        print(f"  add_missing_symbols: Propagating missing data {missing_data} to NSDFG {parent_nsdfg_node.label}")
        _insert_missing_data_through_parent_scopes(missing_data, parent_nsdfg_node, parent_graph, parent_sdfg)

    # Handle remaining pure symbols
    missing_pure_symbols = missing_symbols - missing_data
    for ms in missing_pure_symbols:
        if "tmp_index" in ms:
            continue
        if str(ms).lower() in ("and", "or"):
            continue

        if ms in parent_sdfg.symbols:
            parent_nsdfg_node.symbol_mapping[ms] = ms
            if ms not in sdfg.symbols:
                sdfg.add_symbol(ms, parent_sdfg.symbols[ms])
        elif ms not in sdfg.symbols:
            sdfg.add_symbol(ms, dace.int32)


def add_missing_data_and_symbols_to_all_nsdfgs(sdfg: dace.SDFG):
    """Recursively repair symbol/data mappings for all NestedSDFGs in the hierarchy."""
    for node, graph in sdfg.all_nodes_recursive():
        if isinstance(node, dace.nodes.NestedSDFG):
            add_missing_data_and_symbols(sdfg, graph, graph.sdfg, node.sdfg)
