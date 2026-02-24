import dace
from dace import nodes, dtypes
import copy

# To switch an array (e.g., inv_dual_edge_length) to FP32 external storage,
# you must update the following files:
# 1. include/storage_types.h:
#    Change the typedef (e.g., inv_dual_edge_length_storage_t) to 'float'.
# 2. utils/stages/compile_gpu_stage8.py:
#    Change 'external_dtype' to 'dace.float32' in the inject_pointwise_decompression call.
#
# Note: ICON structs (shared_struct_defs.h) and Serde logic (serde_ref.h)
# automatically use the storage aliases and do not require manual updates.


from typing import Union, Iterable


def inject_pointwise_decompression(
    sdfg: dace.SDFG,
    array_names: Union[str, Iterable[str]],
    external_dtype: dace.typeclass,
):
    """
    Injects a pointwise decompression layer by propagating type change and inserting
    a T -> X shim at every tasklet read/write site.
    """
    if isinstance(array_names, str):
        array_names = [array_names]

    # Expand list to include gpu_ versions if they exist
    all_targets = set()
    for name in array_names:
        if name in sdfg.arrays:
            all_targets.add(name)

        # Check for gpu_ sibling if it's a host array
        if not name.startswith("gpu_"):
            gpu_name = f"gpu_{name}"
            if gpu_name in sdfg.arrays:
                all_targets.add(gpu_name)
        else:
            # Check for host sibling if it's a gpu array
            host_name = name.replace("gpu_", "")
            if host_name in sdfg.arrays:
                all_targets.add(host_name)

    if not all_targets:
        print(
            f"  None of the arrays {array_names} found in SDFG {sdfg.name}, skipping."
        )
        return sdfg

    visited = set()
    for target in all_targets:
        # If already visited (e.g. as a sibling of a previous target), skip
        if (sdfg, target) in visited:
            continue

        print(f"  Injecting pointwise decompression for {target}...")
        internal_dtype = sdfg.arrays[target].dtype
        if internal_dtype == external_dtype:
            print(f"    Array {target} already has type {external_dtype}, skipping.")
            continue

        _update_recursive(sdfg, target, internal_dtype, external_dtype, visited)

    sdfg.validate()
    return sdfg


def _update_recursive(
    sdfg: dace.SDFG,
    array_name: str,
    internal_dtype: dace.typeclass,
    external_dtype: dace.typeclass,
    visited=None,
):
    if visited is None:
        visited = set()

    if (sdfg, array_name) in visited:
        return
    visited.add((sdfg, array_name))

    # Update array descriptor at this level
    assert array_name in sdfg.arrays, (
        f"Array {array_name} not found in SDFG {sdfg.name}"
    )

    if sdfg.arrays[array_name].dtype == external_dtype:
        return

    sdfg.arrays[array_name].dtype = external_dtype

    for state in sdfg.all_states():
        # Iterate over a copy of nodes since we will add new ones
        for node in list(state.nodes()):
            # Handle both directions (Decompression/Reads and Compression/Writes)
            for is_decompression in [True, False]:
                edges = (
                    state.in_edges(node) if is_decompression else state.out_edges(node)
                )
                for edge in list(edges):
                    if edge.data.data == array_name:
                        # Determine if this is a surgical (scalar) or bulk (volume) access
                        is_scalar_access = isinstance(
                            sdfg.arrays[array_name], dace.data.Scalar
                        ) or (
                            edge.data.subset
                            and edge.data.subset.num_elements() == 1
                            and isinstance(node, nodes.Tasklet)
                        )

                        if is_scalar_access and isinstance(node, nodes.Tasklet):
                            if is_decompression:
                                _insert_decompression_shim(
                                    state, node, edge, internal_dtype, external_dtype
                                )
                            else:
                                _insert_compression_shim(
                                    state, node, edge, internal_dtype, external_dtype
                                )

                        elif isinstance(node, nodes.NestedSDFG):
                            # Case 2: Boundary. Update connector and recurse.
                            conn = edge.dst_conn if is_decompression else edge.src_conn
                            _update_recursive(
                                node.sdfg,
                                conn,
                                internal_dtype,
                                external_dtype,
                                visited,
                            )

                        elif isinstance(node, nodes.AccessNode):
                            if node.data == array_name:
                                continue

                            # Case 3: View, CPU/GPU Sibling, or Array-to-Array copy.
                            other_name = node.data
                            is_sibling = other_name.replace(
                                "gpu_", ""
                            ) == array_name.replace("gpu_", "")

                            if (
                                isinstance(sdfg.arrays[other_name], dace.data.View)
                                or is_sibling
                            ):
                                _update_recursive(
                                    sdfg,
                                    other_name,
                                    internal_dtype,
                                    external_dtype,
                                    visited,
                                )
                            else:
                                # Real Array-to-Array Copy
                                _insert_copy_shim(
                                    state,
                                    edge,
                                    internal_dtype,
                                    external_dtype,
                                    is_decompression,
                                )

                        elif isinstance(node, (nodes.MapEntry, nodes.MapExit)):
                            # Structural nodes, skip and let the loop find the actual compute nodes
                            continue

                        elif isinstance(node, nodes.LibraryNode):
                            # TODO: LibraryNode must support the external_dtype.
                            # We skip the shim here to avoid bulk copy overhead or type mismatches.
                            continue

                        else:
                            # Fallback: Bulk Copy Shim for any other unhandled nodes (e.g. direct Array copies)
                            _insert_copy_shim(
                                state,
                                edge,
                                internal_dtype,
                                external_dtype,
                                is_decompression,
                            )


def _insert_decompression_shim(
    state: dace.SDFGState, dst_node, edge, internal_dtype, external_dtype
):
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
        find_new_name=True,
    )

    # 2. Create Decompression Tasklet T
    # Simple cast/assignment
    decomp_tasklet = state.add_tasklet(
        name=f"decomp_{dst_conn}",
        inputs={"_in"},
        outputs={"_out"},
        code=f"_out = static_cast<{internal_dtype.ctype}>(_in);",
        language=dtypes.Language.CPP,
    )

    # 3. Create AccessNode for X
    tmp_node = state.add_access(tmp_name)

    # 4. Reroute
    state.remove_edge(edge)

    # Source -> T (using original indexing/subset)
    state.add_edge(edge.src, edge.src_conn, decomp_tasklet, "_in", old_memlet)

    # T -> X (scalar write)
    state.add_edge(
        decomp_tasklet, "_out", tmp_node, None, dace.Memlet(f"{tmp_name}[0]")
    )

    # X -> Original Destination (scalar read)
    # The original connector type on dst_node remains internal_dtype
    state.add_edge(tmp_node, None, dst_node, dst_conn, dace.Memlet(f"{tmp_name}[0]"))

    print(
        f"  Injected decompression shim T -> {tmp_name} for tasklet '{dst_node.label}'"
    )


def _insert_compression_shim(
    state: dace.SDFGState, src_node, edge, internal_dtype, external_dtype
):
    """
    Reroutes: OriginalSource -> [internal] -> Transient(X) -> [internal] -> Tasklet(T) -> [external] -> Destination
    """
    old_memlet = edge.data
    src_conn = edge.src_conn

    # 1. Create Scalar Transient X
    tmp_name, _ = state.sdfg.add_scalar(
        name=f"t_{src_conn}_to_compress",
        dtype=internal_dtype,
        transient=True,
        find_new_name=True,
    )

    # 2. Create Compression Tasklet T
    comp_tasklet = state.add_tasklet(
        name=f"comp_{src_conn}",
        inputs={"_in"},
        outputs={"_out"},
        code=f"_out = static_cast<{external_dtype.ctype}>(_in);",
        language=dtypes.Language.CPP,
    )

    # 3. Create AccessNode for X
    tmp_node = state.add_access(tmp_name)

    # 4. Reroute
    state.remove_edge(edge)

    # Original Source -> X (scalar write)
    state.add_edge(src_node, src_conn, tmp_node, None, dace.Memlet(f"{tmp_name}[0]"))

    # X -> T (scalar read)
    state.add_edge(tmp_node, None, comp_tasklet, "_in", dace.Memlet(f"{tmp_name}[0]"))

    # T -> Destination (using original indexing/subset)
    state.add_edge(comp_tasklet, "_out", edge.dst, edge.dst_conn, old_memlet)

    print(
        f"  Injected compression shim for tasklet '{src_node.label}' -> T -> {edge.dst.label}"
    )


def _insert_copy_shim(
    state: dace.SDFGState, edge, internal_dtype, external_dtype, is_decompression: bool
):
    """
    Inserts a casting copy between an AccessNode and another Node.
    Ensures a transient buffer is used if the other Node is not an AccessNode.
    """
    sdfg = state.sdfg
    array_node = edge.src if is_decompression else edge.dst
    other_node = edge.dst if is_decompression else edge.src
    array_desc = sdfg.arrays[array_node.data]

    # 1. Ensure the 'other' side is an AccessNode (insert buffer if needed)
    shim_other_node = _ensure_access_node(
        state, edge, other_node, array_desc, is_decompression, internal_dtype
    )

    # 2. Identify source and destination AccessNodes for the cast
    shim_src = array_node if is_decompression else shim_other_node
    shim_dst = shim_other_node if is_decompression else array_node

    # 3. Perform the cast
    if isinstance(array_desc, dace.data.Scalar):
        _add_scalar_cast(
            state, shim_src, shim_dst, internal_dtype, external_dtype, is_decompression
        )
    else:
        _add_array_cast(
            state, shim_src, shim_dst, internal_dtype, external_dtype, is_decompression
        )

    # 4. Cleanup
    state.remove_edge(edge)
    print(
        f"  Injected copy shim for {array_node.data} ({'Decompression' if is_decompression else 'Compression'})"
    )


def _ensure_access_node(
    state: dace.SDFGState, edge, node, array_desc, is_decompression, buffer_dtype
):
    """
    Returns an AccessNode representing 'node'. If 'node' is already an AccessNode, returns it.
    Otherwise, creates a transient buffer, connects it to 'node', and returns the buffer node.
    """
    if isinstance(node, nodes.AccessNode):
        return node

    sdfg = state.sdfg
    name_hint = f"t_{node.label}_shim_buf"

    if isinstance(array_desc, dace.data.Scalar):
        buf_name, _ = sdfg.add_scalar(
            name=name_hint, dtype=buffer_dtype, transient=True, find_new_name=True
        )
    else:
        buf_name, _ = sdfg.add_array(
            name=name_hint,
            shape=array_desc.shape,
            dtype=buffer_dtype,
            transient=True,
            find_new_name=True,
        )

    buf_node = state.add_access(buf_name)
    memlet = dace.Memlet.from_array(buf_name, sdfg.arrays[buf_name])

    if is_decompression:
        # Array -> [Cast] -> Buffer -> OtherNode
        state.add_edge(buf_node, None, node, edge.dst_conn, memlet)
    else:
        # OtherNode -> Buffer -> [Cast] -> Array
        state.add_edge(node, edge.src_conn, buf_node, None, memlet)

    return buf_node


def _add_scalar_cast(
    state, src_node, dst_node, internal_dtype, external_dtype, is_decompression
):
    """Inserts a Tasklet-based scalar cast."""
    target_dtype = internal_dtype if is_decompression else external_dtype
    tasklet = _create_cast_tasklet(state, src_node.data, target_dtype)

    state.add_edge(
        src_node,
        None,
        tasklet,
        "_in",
        dace.Memlet.from_array(src_node.data, state.sdfg.arrays[src_node.data]),
    )
    state.add_edge(
        tasklet,
        "_out",
        dst_node,
        None,
        dace.Memlet.from_array(dst_node.data, state.sdfg.arrays[dst_node.data]),
    )


def _add_array_cast(
    state, src_node, dst_node, internal_dtype, external_dtype, is_decompression
):
    """Inserts a Map-based array cast."""
    sdfg = state.sdfg
    src_desc = sdfg.arrays[src_node.data]
    dst_desc = sdfg.arrays[dst_node.data]
    target_dtype = internal_dtype if is_decompression else external_dtype

    map_ranges = {f"i{i}": f"0:{s}" for i, s in enumerate(src_desc.shape)}
    indices = ", ".join(map_ranges.keys())
    entry, exit = state.add_map(f"cast_{src_node.data}_to_{dst_node.data}", map_ranges)
    tasklet = _create_cast_tasklet(state, src_node.data, target_dtype)

    # Map Connectors
    entry.add_in_connector(f"IN_{src_node.data}")
    entry.add_out_connector(f"OUT_{src_node.data}")
    exit.add_in_connector(f"IN_{dst_node.data}")
    exit.add_out_connector(f"OUT_{dst_node.data}")

    # Wiring
    state.add_edge(
        src_node,
        None,
        entry,
        f"IN_{src_node.data}",
        dace.Memlet.from_array(src_node.data, src_desc),
    )
    state.add_edge(
        entry,
        f"OUT_{src_node.data}",
        tasklet,
        "_in",
        dace.Memlet(f"{src_node.data}[{indices}]"),
    )
    state.add_edge(
        tasklet,
        "_out",
        exit,
        f"IN_{dst_node.data}",
        dace.Memlet(f"{dst_node.data}[{indices}]"),
    )
    state.add_edge(
        exit,
        f"OUT_{dst_node.data}",
        dst_node,
        None,
        dace.Memlet.from_array(dst_node.data, dst_desc),
    )


def _create_cast_tasklet(state, name_hint, target_dtype):
    """Creates a basic C++ casting tasklet."""
    return state.add_tasklet(
        name=f"cast_{name_hint}_shim",
        inputs={"_in"},
        outputs={"_out"},
        code=f"_out = static_cast<{target_dtype.ctype}>(_in);",
        language=dtypes.Language.CPP,
    )
