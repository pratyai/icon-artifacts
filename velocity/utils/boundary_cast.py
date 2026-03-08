"""boundary_cast.py — Insert dtype cast Maps at H2D/D2H boundaries.

Replaces inject_pointwise_decompression for non-transient arrays.
Instead of per-tasklet cast shims throughout the SDFG, a single cast Map
is inserted at the CPU↔GPU copy boundary:

  H2D: AN(cpu, double) → [cast Map] → AN(lowered, float) → AN(gpu, float)
  D2H: AN(gpu, float) → AN(lowered, float) → [cast Map] → AN(cpu, double)

GPU kernels run natively in the lowered dtype — no static_cast at every read.

Called from compile_gpu_stage8.py.
"""

from __future__ import annotations

from typing import Iterable, Union

import dace
from dace import dtypes, nodes


def inject_scalar_boundary_cast(
    sdfg: dace.SDFG,
    scalar_names: Union[str, Iterable[str]],
    external_dtype: dace.typeclass,
) -> dace.SDFG:
    """Lower non-transient scalars inside NestedSDFGs only.

    The top-level scalar stays double (Fortran ABI). Inside each NestedSDFG
    that receives the scalar, we:
    1. Keep the connector array as double (matches parent edge)
    2. Add a transient ``{name}_cast`` with the lowered dtype
    3. Insert a cast tasklet in the NestedSDFG
    4. Replace all internal uses of the scalar with the cast version
    5. Recurse into deeper NestedSDFGs

    This avoids the reinterpret-cast problem (``*(float16*)(&dtime)``)
    that DaCe generates when edge dtype mismatches connector dtype.
    """
    if isinstance(scalar_names, str):
        scalar_names = [scalar_names]

    for name in scalar_names:
        if name not in sdfg.arrays:
            continue

        print(f"  scalar_boundary_cast: {name} → lowered inside NestedSDFGs")

        # Walk all NestedSDFGs in the top-level SDFG that receive this scalar
        for state in sdfg.states():
            for node in state.nodes():
                if not isinstance(node, nodes.NestedSDFG):
                    continue
                # Check input connectors
                for edge in state.in_edges(node):
                    if edge.data.data == name and edge.dst_conn:
                        conn_name = edge.dst_conn
                        _lower_scalar_in_nsdfg(node.sdfg, conn_name, external_dtype)
                        # Rename connector to match the renamed internal array
                        # so that DaCe writes double data into the double array
                        # (not into the lowered transient via reinterpret cast).
                        orig_conn = f"{conn_name}_orig"
                        node.remove_in_connector(conn_name)
                        node.add_in_connector(orig_conn)
                        edge._dst_conn = orig_conn

    sdfg.validate()
    return sdfg


def _lower_scalar_in_nsdfg(
    nsdfg: dace.SDFG,
    scalar_name: str,
    target_dtype: dace.typeclass,
):
    """Lower a scalar inside a NestedSDFG by renaming + casting.

    1. Rename ``scalar_name`` → ``{scalar_name}_orig`` (keeps double dtype)
    2. Add new transient ``scalar_name`` with target_dtype
    3. In each state where ``_orig`` is read, insert a cast tasklet
    4. Recurse into deeper NestedSDFGs
    """
    if scalar_name not in nsdfg.arrays:
        return

    desc = nsdfg.arrays[scalar_name]
    if desc.dtype == target_dtype:
        return  # already lowered

    orig_name = f"{scalar_name}_orig"

    # Rename the connector array: scalar_name → scalar_name_orig
    # This keeps the double dtype for the connector interface.
    nsdfg.arrays[orig_name] = desc
    del nsdfg.arrays[scalar_name]

    # Add new transient with the lowered dtype, reusing the original name
    nsdfg.add_scalar(scalar_name, dtype=target_dtype, transient=True)

    # Rename all AccessNodes and memlets from scalar_name to orig_name
    for st in nsdfg.states():
        for nd in st.nodes():
            if isinstance(nd, nodes.AccessNode) and nd.data == scalar_name:
                nd.data = orig_name
        for edge in st.edges():
            if edge.data.data == scalar_name:
                edge.data.data = orig_name

    # Now insert cast: orig_name (double) → scalar_name (lowered)
    # in each state where orig_name is read
    for st in nsdfg.states():
        orig_access_nodes = [
            nd
            for nd in st.nodes()
            if isinstance(nd, nodes.AccessNode) and nd.data == orig_name
        ]
        if not orig_access_nodes:
            continue

        # Check if any of these ANs have outgoing edges (i.e., the scalar is read)
        has_readers = any(len(st.out_edges(nd)) > 0 for nd in orig_access_nodes)
        if not has_readers:
            continue

        # Add cast chain: AN(orig) → tasklet → AN(scalar_name)
        cast_src = orig_access_nodes[0]
        cast_dst = st.add_access(scalar_name)
        tasklet = st.add_tasklet(
            f"cast_{scalar_name}", {"_in"}, {"_out"}, "_out = _in;"
        )
        st.add_edge(
            cast_src,
            None,
            tasklet,
            "_in",
            dace.Memlet(data=orig_name, subset="0"),
        )
        st.add_edge(
            tasklet,
            "_out",
            cast_dst,
            None,
            dace.Memlet(data=scalar_name, subset="0"),
        )

        # Redirect all edges that read orig_name to read scalar_name instead
        for nd in orig_access_nodes:
            for edge in list(st.out_edges(nd)):
                if edge.dst is tasklet:
                    continue  # don't redirect the cast tasklet's own input
                dst_node = edge.dst
                dst_conn = edge.dst_conn
                st.remove_edge(edge)
                new_memlet = dace.Memlet(data=scalar_name, subset="0")
                st.add_edge(cast_dst, None, dst_node, dst_conn, new_memlet)

        # Fix Map-scope edges: the rename step changed ALL edges to orig_name,
        # but edges inside Map scopes (MapEntry→consumer) should reference
        # scalar_name (the lowered version) since the Map now receives lowered
        # data from the cast output.  Only the cast input edge keeps orig_name.
        for edge in st.edges():
            if edge.data.data == orig_name:
                if edge.dst is tasklet and edge.dst_conn == "_in":
                    continue  # cast tasklet's input stays orig_name
                edge.data.data = scalar_name

        # Clean up orphaned orig ANs (keep cast_src since it feeds the tasklet)
        for nd in orig_access_nodes:
            if nd is not cast_src and st.degree(nd) == 0:
                st.remove_node(nd)

    # Recurse: propagate dtype into deeper NestedSDFGs that receive scalar_name.
    # The cast already happened at this level, so deeper NSFGs receive lowered
    # data. Just change their internal array dtype to match (no extra cast needed).
    for st in nsdfg.states():
        for nd in st.nodes():
            if not isinstance(nd, nodes.NestedSDFG):
                continue
            for edge in st.in_edges(nd):
                if edge.data.data == scalar_name and edge.dst_conn:
                    _propagate_dtype(nd.sdfg, edge.dst_conn, target_dtype)


def inject_boundary_cast(
    sdfg: dace.SDFG,
    array_names: Union[str, Iterable[str]],
    external_dtype: dace.typeclass,
    permutation: list[int] | None = None,
) -> dace.SDFG:
    """Insert boundary cast Maps for the given arrays.

    For each array:
    1. Create a lowered CPU transient (external_dtype)
    2. Change the GPU array dtype to external_dtype
    3. Insert a cast Map at the H2D boundary
    4. Insert a reverse cast Map at the D2H boundary (if present)
    5. Redirect H2D/D2H edges through the lowered transient

    Args:
        sdfg: The SDFG to transform.
        array_names: CPU array names (e.g. ``__CG_p_metrics__m_ddqz_z_half``).
        external_dtype: Target dtype (e.g. ``dace.float32``).
        permutation: Optional dimension permutation for the GPU-side array.

    Returns:
        The modified SDFG.
    """
    if isinstance(array_names, str):
        array_names = [array_names]

    for name in array_names:
        cpu_name = name
        gpu_name = f"gpu_{name}"

        if cpu_name not in sdfg.arrays:
            print(f"  boundary_cast: {cpu_name} not in SDFG, skipping.")
            continue
        if gpu_name not in sdfg.arrays:
            print(f"  boundary_cast: {gpu_name} not in SDFG, skipping.")
            continue

        cpu_arr = sdfg.arrays[cpu_name]
        orig_dtype = cpu_arr.dtype

        # If already lowered AND no permutation is requested, skip
        if orig_dtype == external_dtype and not permutation:
            print(f"  boundary_cast: {cpu_name} already {external_dtype}, skipping.")
            continue

        _inject_for_array(
            sdfg, cpu_name, gpu_name, orig_dtype, external_dtype, permutation
        )

    sdfg.validate()
    return sdfg


def _get_fortran_strides(
    shape: tuple[dace.symbolic.SymbolicType, ...],
) -> tuple[dace.symbolic.SymbolicType, ...]:
    accum = 1
    strides = []
    for s in shape:
        strides.append(accum)
        accum *= s
    return tuple(strides)


def _propagate_permutation(
    sdfg: dace.SDFG,
    array_name: str,
    permutation: list[int],
    visited: set | None = None,
):
    """Recursively change an array's layout (permutation) through all NestedSDFGs.

    Updates:
    - The array descriptor shape and strides at each level
    - All memlets referencing the array (permuting their subset indices)
    - NestedSDFG connector-mapped internal arrays (recursively)
    """
    if visited is None:
        visited = set()
    key = (id(sdfg), array_name)
    if key in visited:
        return
    visited.add(key)

    if array_name not in sdfg.arrays:
        return

    desc = sdfg.arrays[array_name]
    if not isinstance(desc, dace.data.Array):
        return

    # Check if the array has enough dimensions for the permutation
    # (e.g. if permutation is [1, 0], array must have at least 2 dims)
    if len(desc.shape) < len(permutation):
        return

    # Pad permutation if necessary (identity for trailing dims)
    p = permutation + list(range(len(permutation), len(desc.shape)))
    if len(p) > len(desc.shape):
        p = p[: len(desc.shape)]

    # Detect major-order style to preserve it (Fortran vs C)
    is_fortran = (desc.strides[0] == 1) if len(desc.strides) > 0 else False

    # Update descriptor shape
    orig_shape = desc.shape
    desc.shape = tuple(orig_shape[i] for i in p)

    # Recalculate strides based on style
    if is_fortran:
        desc.strides = _get_fortran_strides(desc.shape)
    else:
        # Default to C-style (last dim contiguous)
        desc.strides = desc.strides_from_layout(*range(len(desc.shape)))[0]

    # Update all memlets in this SDFG referencing this array
    for state in sdfg.states():
        for edge in state.edges():
            if (
                edge.data is not None
                and edge.data.data == array_name
                and edge.data.subset is not None
            ):
                # Permute the memlet subset
                new_subset = []
                # Ensure we have enough indices in the subset
                # (Scalar accesses might have fewer indices)
                for i in p:
                    if i < len(edge.data.subset):
                        new_subset.append(edge.data.subset[i])
                edge.data.subset = dace.subsets.Range(new_subset)

    # Recurse into NestedSDFGs
    for state in sdfg.states():
        for node in state.nodes():
            if not isinstance(node, nodes.NestedSDFG):
                continue
            # Check input/output connectors
            for edge in state.in_edges(node):
                if edge.data.data == array_name and edge.dst_conn:
                    _propagate_permutation(node.sdfg, edge.dst_conn, p, visited)
            for edge in state.out_edges(node):
                if edge.data.data == array_name and edge.src_conn:
                    _propagate_permutation(node.sdfg, edge.src_conn, p, visited)


def _propagate_dtype(
    sdfg: dace.SDFG,
    array_name: str,
    new_dtype: dace.typeclass,
    visited: set | None = None,
):
    """Recursively change an array's dtype through all NestedSDFGs.

    When a GPU array's dtype is changed at the top level, NestedSDFGs that
    access it via connectors still have the old dtype in their internal
    array descriptors.  DaCe codegen uses the internal descriptors for
    kernel parameter types, so they must match the allocation dtype.

    This function walks the SDFG hierarchy and updates:
    - The array descriptor dtype at each level
    - NestedSDFG connector-mapped internal arrays (recursively)
    """
    if visited is None:
        visited = set()
    key = (id(sdfg), array_name)
    if key in visited:
        return
    visited.add(key)

    if array_name not in sdfg.arrays:
        return
    sdfg.arrays[array_name].dtype = new_dtype

    for state in sdfg.states():
        for node in state.nodes():
            if not isinstance(node, nodes.NestedSDFG):
                continue
            # Check if this NestedSDFG has a connector matching array_name
            for edge in state.in_edges(node):
                if edge.data.data == array_name and edge.dst_conn:
                    _propagate_dtype(node.sdfg, edge.dst_conn, new_dtype, visited)
            for edge in state.out_edges(node):
                if edge.data.data == array_name and edge.src_conn:
                    _propagate_dtype(node.sdfg, edge.src_conn, new_dtype, visited)


def _inject_for_array(
    sdfg: dace.SDFG,
    cpu_name: str,
    gpu_name: str,
    orig_dtype: dace.typeclass,
    external_dtype: dace.typeclass,
    permutation: list[int] | None = None,
):
    """Apply boundary cast for a single cpu/gpu array pair."""
    cpu_arr = sdfg.arrays[cpu_name]

    # Step 1: Create lowered CPU transient
    lowered_name = f"{cpu_name}_lowered"

    # Calculate permuted shape for the lowered transient and GPU array
    shape = cpu_arr.shape
    if permutation:
        p = permutation + list(range(len(permutation), len(shape)))
        if len(p) > len(shape):
            p = p[: len(shape)]
        shape = tuple(shape[i] for i in p)

    sdfg.add_array(
        lowered_name,
        shape=shape,
        dtype=external_dtype,
        transient=True,
        storage=dtypes.StorageType.CPU_Heap,
    )

    # Step 2: Change GPU array dtype AND layout — must propagate through NestedSDFGs
    # so that kernel parameter types and indexing match the allocation.
    _propagate_dtype(sdfg, gpu_name, external_dtype)
    if permutation:
        _propagate_permutation(sdfg, gpu_name, permutation)

    # Step 2b: Ensure lowered CPU transient matches GPU layout (same shape and strides)
    # to enable simple linear memcpy.
    lowered_arr = sdfg.arrays[lowered_name]
    gpu_arr = sdfg.arrays[gpu_name]
    lowered_arr.strides = gpu_arr.strides

    print(
        f"  boundary_cast: {cpu_name} ({orig_dtype}) → "
        f"{lowered_name} ({external_dtype}, permute={permutation}) → {gpu_name} ({external_dtype})"
    )

    # Step 3 & 4: Find H2D and D2H edges, insert cast Maps
    for state in sdfg.states():
        edges_to_process = []
        for edge in state.edges():
            src = edge.src
            dst = edge.dst
            if not (
                isinstance(src, nodes.AccessNode) and isinstance(dst, nodes.AccessNode)
            ):
                continue

            # H2D: cpu_name → gpu_name
            if src.data == cpu_name and dst.data == gpu_name:
                edges_to_process.append(("h2d", edge))

            # D2H: gpu_name → cpu_name
            if src.data == gpu_name and dst.data == cpu_name:
                edges_to_process.append(("d2h", edge))

        for direction, edge in edges_to_process:
            if direction == "h2d":
                _insert_h2d_cast(
                    sdfg,
                    state,
                    edge,
                    cpu_name,
                    gpu_name,
                    lowered_name,
                    orig_dtype,
                    external_dtype,
                    permutation,
                )
            else:
                _insert_d2h_cast(
                    sdfg,
                    state,
                    edge,
                    cpu_name,
                    gpu_name,
                    lowered_name,
                    orig_dtype,
                    external_dtype,
                    permutation,
                )


def _insert_h2d_cast(
    sdfg: dace.SDFG,
    state: dace.SDFGState,
    edge,
    cpu_name: str,
    gpu_name: str,
    lowered_name: str,
    orig_dtype: dace.typeclass,
    external_dtype: dace.typeclass,
    permutation: list[int] | None = None,
):
    """Replace ``AN(cpu) → AN(gpu)`` with ``AN(cpu) → [cast Map] → AN(lowered) → AN(gpu)``.

    Before:
      AN(cpu_name, double) ──[memcpy H2D]──> AN(gpu_name, double)

    After:
      AN(cpu_name, double) ──> [cast Map double→float + permute] ──> AN(lowered, float)
      AN(lowered, float) ──[memcpy H2D]──> AN(gpu_name, float)
    """
    cpu_an = edge.src
    gpu_an = edge.dst

    # Remove the old direct H2D edge
    state.remove_edge(edge)

    # Create AccessNode for lowered transient
    lowered_an = state.add_access(lowered_name)

    # Build cast Map: cpu_name (orig_dtype) → lowered_name (external_dtype)
    cpu_arr = sdfg.arrays[cpu_name]
    _add_cast_map(
        sdfg,
        state,
        cpu_an,
        cpu_name,
        lowered_an,
        lowered_name,
        cpu_arr.shape,
        external_dtype,
        "h2d",
        permutation,
    )

    # New H2D edge: lowered → gpu (both external_dtype and permuted layout)
    lowered_arr = sdfg.arrays[lowered_name]
    state.add_edge(
        lowered_an,
        None,
        gpu_an,
        None,
        dace.Memlet.from_array(lowered_name, lowered_arr),
    )

    print(f"    Inserted H2D cast Map for {cpu_name}")


def _insert_d2h_cast(
    sdfg: dace.SDFG,
    state: dace.SDFGState,
    edge,
    cpu_name: str,
    gpu_name: str,
    lowered_name: str,
    orig_dtype: dace.typeclass,
    external_dtype: dace.typeclass,
    permutation: list[int] | None = None,
):
    """Replace ``AN(gpu) → AN(cpu)`` with ``AN(gpu) → AN(lowered) → [cast Map] → AN(cpu)``.

    Before:
      AN(gpu_name, float) ──[memcpy D2H]──> AN(cpu_name, double)

    After:
      AN(gpu_name, float) ──[memcpy D2H]──> AN(lowered, float)
      AN(lowered, float) ──> [cast Map float→double + un-permute] ──> AN(cpu_name, double)
    """
    gpu_an = edge.src
    cpu_an = edge.dst

    # Remove the old direct D2H edge
    state.remove_edge(edge)

    # Create AccessNode for lowered transient
    lowered_an = state.add_access(lowered_name)

    # D2H edge: gpu → lowered (both external_dtype and permuted layout)
    lowered_arr = sdfg.arrays[lowered_name]
    state.add_edge(
        gpu_an,
        None,
        lowered_an,
        None,
        dace.Memlet.from_array(lowered_name, lowered_arr),
    )

    # Build cast Map: lowered_name (external_dtype) → cpu_name (orig_dtype)
    cpu_arr = sdfg.arrays[cpu_name]
    _add_cast_map(
        sdfg,
        state,
        lowered_an,
        lowered_name,
        cpu_an,
        cpu_name,
        cpu_arr.shape,
        orig_dtype,
        "d2h",
        permutation,
    )

    print(f"    Inserted D2H cast Map for {cpu_name}")


def _add_cast_map(
    sdfg: dace.SDFG,
    state: dace.SDFGState,
    src_an: nodes.AccessNode,
    src_name: str,
    dst_an: nodes.AccessNode,
    dst_name: str,
    shape: tuple,
    target_dtype: dace.typeclass,
    tag: str,
    permutation: list[int] | None = None,
):
    """Add a Map that casts every element from src to dst.

    The cast tasklet emits ``static_cast<target_dtype>(_in)``.
    """
    src_desc = sdfg.arrays[src_name]
    dst_desc = sdfg.arrays[dst_name]

    map_ranges = {f"__i{i}": f"0:{s}" for i, s in enumerate(shape)}
    indices = [f"__i{i}" for i in range(len(shape))]

    src_indices = ", ".join(indices)
    dst_indices = src_indices

    if permutation:
        p = permutation + list(range(len(permutation), len(shape)))
        if len(p) > len(shape):
            p = p[: len(shape)]
        permuted_indices = [indices[i] for i in p]
        if tag == "h2d":
            # H2D: src is original CPU, dst is permuted lowered
            dst_indices = ", ".join(permuted_indices)
        else:
            # D2H: src is permuted lowered, dst is original CPU
            src_indices = ", ".join(permuted_indices)

    map_entry, map_exit = state.add_map(f"boundary_cast_{tag}_{src_name}", map_ranges)

    tasklet = state.add_tasklet(
        name=f"cast_{tag}_{src_name}",
        inputs={"_in"},
        outputs={"_out"},
        code=f"_out = static_cast<{target_dtype.ctype}>(_in);",
        language=dtypes.Language.CPP,
    )

    # Map entry connectors
    in_conn = f"IN_{src_name}"
    out_conn = f"OUT_{src_name}"
    map_entry.add_in_connector(in_conn)
    map_entry.add_out_connector(out_conn)

    # Map exit connectors
    exit_in_conn = f"IN_{dst_name}"
    exit_out_conn = f"OUT_{dst_name}"
    map_exit.add_in_connector(exit_in_conn)
    map_exit.add_out_connector(exit_out_conn)

    # Wire: src_an → map_entry
    state.add_edge(
        src_an,
        None,
        map_entry,
        in_conn,
        dace.Memlet.from_array(src_name, src_desc),
    )

    # Wire: map_entry → tasklet
    state.add_edge(
        map_entry,
        out_conn,
        tasklet,
        "_in",
        dace.Memlet(f"{src_name}[{src_indices}]"),
    )

    # Wire: tasklet → map_exit
    state.add_edge(
        tasklet,
        "_out",
        map_exit,
        exit_in_conn,
        dace.Memlet(f"{dst_name}[{dst_indices}]"),
    )

    # Wire: map_exit → dst_an
    state.add_edge(
        map_exit,
        exit_out_conn,
        dst_an,
        None,
        dace.Memlet.from_array(dst_name, dst_desc),
    )
