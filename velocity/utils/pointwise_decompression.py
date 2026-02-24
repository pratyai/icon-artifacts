import dace
from dace import nodes, dtypes

def inject_pointwise_decompression(sdfg: dace.SDFG, array_name: str, external_dtype: dace.typeclass):
    """
    Injects a pointwise decompression layer by propagating type change and inserting 
    a T -> X shim at every tasklet read site.
    """
    if array_name not in sdfg.arrays:
        print(f"  Array {array_name} not found in SDFG {sdfg.name}, skipping.")
        return sdfg

    internal_dtype = sdfg.arrays[array_name].dtype
    _update_recursive(sdfg, array_name, internal_dtype, external_dtype)

    sdfg.validate()
    return sdfg

def _update_recursive(sdfg: dace.SDFG, array_name: str, internal_dtype: dace.typeclass, external_dtype: dace.typeclass):
    # Update array descriptor at this level
    assert array_name in sdfg.arrays, f"Array {array_name} not found in SDFG {sdfg.name}"
    sdfg.arrays[array_name].dtype = external_dtype
    
    for state in sdfg.states():
        # Iterate over a copy of nodes since we will add new ones
        for node in list(state.nodes()):
            # Find incoming edges that consume this data
            for edge in list(state.in_edges(node)):
                if edge.data.data == array_name:
                    if isinstance(node, nodes.Tasklet):
                        # Case 1: Leaf-level read. Insert T -> X -> node shim.
                        _insert_shim(state, node, edge, internal_dtype, external_dtype)
                    
                    elif isinstance(node, nodes.NestedSDFG):
                        # Case 2: Boundary. Update connector and recurse.
                        internal_name = edge.dst_conn
                        _update_recursive(node.sdfg, internal_name, internal_dtype, external_dtype)

def _insert_shim(state: dace.SDFGState, dst_node, edge, internal_dtype, external_dtype):
    """
    Reroutes: Source -> [external] -> Tasklet(T) -> [internal] -> Transient(X) -> [internal] -> OriginalDest
    """
    old_memlet = edge.data
    dst_conn = edge.dst_conn
    
    # 1. Create Scalar Transient X
    tmp_name, _ = state.sdfg.add_scalar(
        name=f"t_{dst_conn}_decompressed",
        dtype=internal_dtype,
        transient=True,
        find_new_name=True
    )
    
    # 2. Create Decompression Tasklet T
    # Simple cast/assignment
    decomp_tasklet = state.add_tasklet(
        name=f"decomp_{dst_conn}",
        inputs={"_in"},
        outputs={"_out"},
        code=f"_out = static_cast<{internal_dtype.ctype}>(_in);",
        language=dtypes.Language.CPP
    )
    
    # 3. Create AccessNode for X
    tmp_node = state.add_access(tmp_name)
    
    # 4. Reroute
    state.remove_edge(edge)
    
    # Source -> T (using original indexing/subset)
    state.add_edge(edge.src, edge.src_conn, decomp_tasklet, "_in", old_memlet)
    
    # T -> X (scalar write)
    state.add_edge(decomp_tasklet, "_out", tmp_node, None, dace.Memlet(f"{tmp_name}[0]"))
    
    # X -> Original Destination (scalar read)
    # The original connector type on dst_node remains internal_dtype
    state.add_edge(tmp_node, None, dst_node, dst_conn, dace.Memlet(f"{tmp_name}[0]"))

    print(f"  Injected shim T -> {tmp_name} for tasklet '{dst_node.label}'")
