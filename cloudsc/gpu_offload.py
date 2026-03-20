"""gpu_offload.py — Transform a CPU-optimized SDFG for GPU execution.

Creates gpu_ copies of non-transient arrays, adds H2D/D2H copy states,
sets GPU schedules on outermost Maps, and renames references inside
GPU-scheduled scopes to use GPU arrays.

Usage:
    python gpu_offload.py --sdfg cloudsc_opt.sdfgz -o cloudsc_gpu.sdfgz
"""

from __future__ import annotations

import argparse
import copy
import re
from typing import Set

import dace
from dace import dtypes, nodes


# ---------------------------------------------------------------------------
# Step 1: Classify arrays
# ---------------------------------------------------------------------------

def _classify_arrays(sdfg: dace.SDFG, verbose: bool):
    """Partition SDFG data into categories for GPU offloading.

    Returns:
        non_trans_arrays: non-transient arrays (need H2D/D2H)
        written_arrays: subset of non_trans_arrays that are written
        trans_arrays: transient arrays (change storage directly)
        interstate_arrays: arrays referenced on interstate edges
    """
    non_trans_arrays = []
    trans_arrays = []

    # Find data that are written (need D2H copy-out)
    written = set()
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nodes.AccessNode) and state.in_degree(node) > 0:
                if node.data in sdfg.arrays and not sdfg.arrays[node.data].transient:
                    for e in state.in_edges(node):
                        if e.data is not None and e.data.data is not None:
                            written.add(node.data)

    for name, desc in sdfg.arrays.items():
        if isinstance(desc, dace.data.Scalar):
            # Scalars only need a GPU copy if they are written to inside GPU scopes.
            # If only read, they are passed by value as kernel parameters.
            if name not in written:
                continue
        
        if isinstance(desc, dace.data.Array):
            if desc.transient:
                trans_arrays.append(name)
            else:
                non_trans_arrays.append(name)

    # Find arrays referenced on interstate edges
    interstate = set()
    for edge in sdfg.all_interstate_edges():
        for sym in edge.data.free_symbols:
            sym_str = str(sym)
            if sym_str in sdfg.arrays:
                interstate.add(sym_str)

    written_arrays = [n for n in non_trans_arrays if n in written]

    if verbose:
        print(f"  Non-transient arrays: {len(non_trans_arrays)}")
        print(f"  Written (need D2H): {len(written_arrays)}")
        print(f"  Transient arrays: {len(trans_arrays)}")
        print(f"  Interstate-referenced: {interstate}")

    return non_trans_arrays, written_arrays, trans_arrays, interstate


# ---------------------------------------------------------------------------
# Step 2: Create GPU descriptors
# ---------------------------------------------------------------------------

def _create_gpu_descriptors(sdfg: dace.SDFG, array_names: list[str], verbose: bool):
    """Create gpu_{name} transient descriptors with GPU_Global storage."""
    for name in array_names:
        gpu_name = f"gpu_{name}"
        if gpu_name in sdfg.arrays:
            continue
        desc = sdfg.arrays[name]
        gpu_desc = copy.deepcopy(desc)
        gpu_desc.transient = True
        gpu_desc.storage = dtypes.StorageType.GPU_Global
        sdfg.add_datadesc(gpu_name, gpu_desc)

    if verbose:
        print(f"  Created {len(array_names)} GPU array descriptors")


# ---------------------------------------------------------------------------
# Step 3 & 4: H2D / D2H copy states
# ---------------------------------------------------------------------------

def _add_h2d_state(sdfg: dace.SDFG, array_names: list[str]):
    """Add a start state that copies all non-transient arrays to GPU."""
    prev_start = sdfg.start_block
    h2d_state = sdfg.add_state("copy_in_h2d", is_start_state=True)
    sdfg.add_edge(h2d_state, prev_start, dace.InterstateEdge())

    for name in array_names:
        gpu_name = f"gpu_{name}"
        gpu_desc = sdfg.arrays[gpu_name]
        src = h2d_state.add_access(name)
        dst = h2d_state.add_access(gpu_name)
        h2d_state.add_edge(src, None, dst, None,
                           dace.Memlet.from_array(name, gpu_desc))

    print(f"  H2D state: {len(array_names)} arrays")


def _add_d2h_state(sdfg: dace.SDFG, written_arrays: list[str]):
    """Add a sink state that copies written arrays back from GPU."""
    sinks = sdfg.sink_nodes()
    d2h_state = sdfg.add_state("copy_out_d2h")

    for sink in sinks:
        sdfg.add_edge(sink, d2h_state, dace.InterstateEdge())

    for name in written_arrays:
        gpu_name = f"gpu_{name}"
        gpu_desc = sdfg.arrays[gpu_name]
        src = d2h_state.add_access(gpu_name)
        dst = d2h_state.add_access(name)
        d2h_state.add_edge(src, None, dst, None,
                           dace.Memlet.from_array(gpu_name, gpu_desc))

    print(f"  D2H state: {len(written_arrays)} arrays")


# ---------------------------------------------------------------------------
# Step 5: Set GPU schedules
# ---------------------------------------------------------------------------

def _set_gpu_schedules(sdfg: dace.SDFG, verbose: bool):
    """Set outermost Maps to GPU_Device schedule."""
    count = 0
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if isinstance(node, nodes.MapEntry) and scope[node] is None:
                node.map.schedule = dtypes.ScheduleType.GPU_Device
                count += 1
    print(f"  GPU-scheduled {count} outermost maps")


# ---------------------------------------------------------------------------
# Step 6: Rename references inside GPU scopes
# ---------------------------------------------------------------------------

def _get_gpu_rename_set(sdfg: dace.SDFG) -> Set[str]:
    """Return the set of array names that have gpu_ counterparts."""
    return {name for name in sdfg.arrays
            if not name.startswith("gpu_") and f"gpu_{name}" in sdfg.arrays}


def _rename_in_gpu_maps(sdfg: dace.SDFG, gpu_arrays: Set[str], verbose: bool):
    """Rename array references to gpu_ versions inside GPU-scheduled map scopes."""
    count = 0
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if not isinstance(node, nodes.MapEntry):
                continue
            if scope[node] is not None:
                continue  # not outermost
            map_exit = state.exit_node(node)
            inner_nodes = set(state.all_nodes_between(node, map_exit))
            inner_nodes.add(node)
            inner_nodes.add(map_exit)

            # Include AccessNodes connected to MapEntry inputs and MapExit outputs
            for e in state.in_edges(node):
                if isinstance(e.src, nodes.AccessNode):
                    inner_nodes.add(e.src)
            for e in state.out_edges(map_exit):
                if isinstance(e.dst, nodes.AccessNode):
                    inner_nodes.add(e.dst)

            # Collect all edges touching inner nodes
            inner_edges = set()
            for n in inner_nodes:
                for e in state.in_edges(n):
                    inner_edges.add(e)
                for e in state.out_edges(n):
                    inner_edges.add(e)

            _rename_edges(sdfg, state, inner_edges, gpu_arrays)
            _rename_access_nodes(inner_nodes, gpu_arrays)
            _rename_nsdfg_connectors(sdfg, state, inner_nodes, gpu_arrays)
            count += 1

    if verbose:
        print(f"  Renamed references in {count} outermost map scopes")


def _rename_edges(sdfg, state, edges, gpu_arrays):
    """Rename memlet .data fields to gpu_ versions."""
    for edge in edges:
        if edge.data is not None and edge.data.data in gpu_arrays:
            edge.data.data = f"gpu_{edge.data.data}"


def _rename_access_nodes(inner_nodes, gpu_arrays):
    """Rename AccessNode .data fields to gpu_ versions."""
    for node in inner_nodes:
        if isinstance(node, nodes.AccessNode) and node.data in gpu_arrays:
            node.data = f"gpu_{node.data}"


def _rename_nsdfg_connectors(sdfg, state, inner_nodes, gpu_arrays):
    """Rename NestedSDFG connectors and propagate gpu_ descriptors inside."""
    for node in inner_nodes:
        if not isinstance(node, nodes.NestedSDFG):
            continue

        # Rename in-connectors
        for old_conn in list(node.in_connectors):
            if old_conn in gpu_arrays:
                new_conn = f"gpu_{old_conn}"
                node.remove_in_connector(old_conn)
                node.add_in_connector(new_conn, force=True)
                _ensure_gpu_desc_in_nsdfg(node.sdfg, old_conn, new_conn,
                                          sdfg.arrays[new_conn])
                for e in state.in_edges(node):
                    if e.dst_conn == old_conn:
                        e._dst_conn = new_conn

        # Rename out-connectors
        for old_conn in list(node.out_connectors):
            if old_conn in gpu_arrays:
                new_conn = f"gpu_{old_conn}"
                node.remove_out_connector(old_conn)
                # force=True: NestedSDFGs may have the same name in both
                # in_connectors and out_connectors
                node.add_out_connector(new_conn, force=True)
                _ensure_gpu_desc_in_nsdfg(node.sdfg, old_conn, new_conn,
                                          sdfg.arrays[new_conn])
                for e in state.out_edges(node):
                    if e.src_conn == old_conn:
                        e._src_conn = new_conn

        # Recurse into the nested SDFG
        _rename_in_nested_sdfg(node.sdfg, gpu_arrays)


def _ensure_gpu_desc_in_nsdfg(nsdfg, old_name, new_name, parent_desc):
    """Ensure the nested SDFG has a matching descriptor for the gpu_ array."""
    if old_name in nsdfg.arrays:
        nsdfg.remove_data(old_name, validate=False)
    if new_name not in nsdfg.arrays:
        desc = copy.deepcopy(parent_desc)
        desc.transient = False
        desc.storage = dtypes.StorageType.GPU_Global
        nsdfg.add_datadesc(new_name, desc)


def _rename_in_nested_sdfg(nsdfg, gpu_arrays):
    """Recursively rename all references inside a nested SDFG."""
    for state in nsdfg.all_states():
        all_edges = set(state.edges())
        all_nodes = set(state.nodes())
        _rename_edges(nsdfg, state, all_edges, gpu_arrays)
        _rename_access_nodes(all_nodes, gpu_arrays)
        _rename_nsdfg_connectors(nsdfg, state, all_nodes, gpu_arrays)

    # Rename on interstate edges (word-boundary safe)
    for edge in nsdfg.all_interstate_edges():
        if edge.data.assignments:
            new_asgn = {}
            for k, v in edge.data.assignments.items():
                new_k = f"gpu_{k}" if k in gpu_arrays else k
                new_v = str(v)
                for name in gpu_arrays:
                    new_v = re.sub(r'\b' + re.escape(name) + r'\b',
                                   f"gpu_{name}", new_v)
                new_asgn[new_k] = new_v
            edge.data.assignments = new_asgn


# ---------------------------------------------------------------------------
# Step 7: Set GPU storage for transient arrays inside GPU map scopes
# ---------------------------------------------------------------------------

def _set_transient_storage(sdfg: dace.SDFG, verbose: bool):
    """Move transient Arrays to GPU_Global inside GPU-scheduled scopes.

    Walks all states of the SDFG.  For each GPU-scheduled outermost map,
    sets transient Array descriptors to GPU_Global, and recursively fixes
    storage inside NestedSDFGs.

    Scalars at the parent level are NOT changed — they are passed by value.
    Inside NestedSDFGs, scalars become Register and arrays become GPU_Global.

    Arrays also accessed on the host (outside any map) are set to
    CPU_Pinned if also GPU-accessed (zero-copy), or left as Default
    if host-only.
    """
    # Find arrays accessed at top-level scope (host side)
    host_accessed = set()
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if isinstance(node, nodes.AccessNode) and node.data in sdfg.arrays:
                if scope[node] is None:
                    # Check it's not just a MapEntry/MapExit boundary node
                    is_boundary = False
                    for e in state.out_edges(node):
                        if isinstance(e.dst, nodes.MapEntry):
                            is_boundary = True
                    for e in state.in_edges(node):
                        if isinstance(e.src, nodes.MapExit):
                            is_boundary = True
                    if not is_boundary:
                        host_accessed.add(node.data)

    # Find arrays accessed inside GPU map scopes (including boundary nodes)
    gpu_accessed = _find_gpu_accessed_arrays(sdfg)

    # Dual-access transients: accessed on BOTH host and GPU
    # Use CPU_Pinned (cudaMallocHost) — accessible from both sides without copies
    dual_transients = set()
    for name in host_accessed & gpu_accessed:
        desc = sdfg.arrays.get(name)
        if desc and desc.transient and not name.startswith("gpu_"):
            if isinstance(desc, dace.data.Array):
                desc.storage = dtypes.StorageType.CPU_Pinned
                dual_transients.add(name)

    count = 0

    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if not isinstance(node, nodes.MapEntry):
                continue
            if scope[node] is not None:
                continue  # not outermost
            if node.map.schedule != dtypes.ScheduleType.GPU_Device:
                continue

            map_exit = state.exit_node(node)
            inner = set(state.all_nodes_between(node, map_exit))
            # Also include boundary AccessNodes (MapEntry inputs, MapExit outputs)
            for e in state.in_edges(node):
                if isinstance(e.src, nodes.AccessNode):
                    inner.add(e.src)
            for e in state.out_edges(map_exit):
                if isinstance(e.dst, nodes.AccessNode):
                    inner.add(e.dst)

            for n in inner:
                if isinstance(n, nodes.AccessNode) and n.data in sdfg.arrays:
                    desc = sdfg.arrays[n.data]
                    # Only move transients not also accessed on host
                    if (desc.transient and not n.data.startswith("gpu_")
                            and n.data not in host_accessed):
                        if isinstance(desc, dace.data.Array) and desc.storage != dtypes.StorageType.GPU_Global:
                            desc.storage = dtypes.StorageType.GPU_Global
                            count += 1
                        elif isinstance(desc, dace.data.Scalar) and desc.storage != dtypes.StorageType.Register:
                            desc.storage = dtypes.StorageType.Register
                            count += 1

                # Recurse into NestedSDFGs — set ALL descriptors to GPU
                if isinstance(n, nodes.NestedSDFG):
                    count += _set_nsdfg_gpu_storage(n.sdfg)

    if verbose:
        print(f"  Dual-access transients (CPU_Pinned): {dual_transients}")
        print(f"  Moved {count} descriptors to GPU storage")


def _set_nsdfg_gpu_storage(nsdfg: dace.SDFG) -> int:
    """Recursively set all descriptors inside a GPU-nested SDFG.

    - Scalars → Register  (passed by value / local to thread)
    - Arrays  → GPU_Global
    """
    count = 0
    for _, desc in nsdfg.arrays.items():
        if desc.storage in (dtypes.StorageType.GPU_Global, dtypes.StorageType.Register):
            continue
        if isinstance(desc, dace.data.Scalar):
            desc.storage = dtypes.StorageType.Register
            count += 1
        elif isinstance(desc, dace.data.Array):
            desc.storage = dtypes.StorageType.GPU_Global
            count += 1

    for state in nsdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nodes.NestedSDFG):
                count += _set_nsdfg_gpu_storage(node.sdfg)
    return count


# ---------------------------------------------------------------------------
# Step 8: Transify non-transient scalars written inside GPU kernels
# ---------------------------------------------------------------------------

def _transify_gpu_scalars(sdfg: dace.SDFG, verbose: bool):
    """Convert non-transient scalars written inside GPU maps to transient Register.

    CUDA kernels cannot write to host-side non-transient scalars.
    """
    count = 0

    def _transify_in_sdfg(current_sdfg, in_gpu_scope=False):
        nonlocal count
        for name, desc in list(current_sdfg.arrays.items()):
            if not isinstance(desc, dace.data.Scalar):
                continue
            if desc.transient:
                continue
            
            # Check if this scalar is written inside a GPU map scope in this SDFG
            written_in_gpu = False
            for st in current_sdfg.all_states():
                scope = st.scope_dict()
                for nd in st.nodes():
                    if isinstance(nd, nodes.AccessNode) and nd.data == name:
                        # If this SDFG is already inside a GPU scope, any write is a GPU write
                        # Otherwise, check if the AccessNode is inside a GPU-scheduled map
                        is_gpu_write = False
                        if in_gpu_scope:
                            is_gpu_write = True
                        else:
                            parent = scope[nd]
                            if parent is not None:
                                outermost = parent
                                while scope[outermost] is not None:
                                    outermost = scope[outermost]
                                if isinstance(outermost, nodes.MapEntry) and outermost.map.schedule == dtypes.ScheduleType.GPU_Device:
                                    is_gpu_write = True
                        
                        if is_gpu_write and any(not e.data.is_empty() for e in st.in_edges(nd)):
                            written_in_gpu = True
                            break
                if written_in_gpu:
                    break
            
            if written_in_gpu:
                # If it's a non-transient scalar written in GPU, and it doesn't have
                # a gpu_ counterpart (handled by H2D/D2H), transify it.
                if f"gpu_{name}" not in sdfg.arrays:
                    desc.transient = True
                    desc.storage = dtypes.StorageType.Register
                    count += 1

        # Recurse into NestedSDFGs
        for st in current_sdfg.all_states():
            scope = st.scope_dict()
            for nd in st.nodes():
                if isinstance(nd, nodes.NestedSDFG):
                    child_in_gpu = in_gpu_scope
                    if not child_in_gpu:
                        parent = scope[nd]
                        if parent is not None:
                            outermost = parent
                            while scope[outermost] is not None:
                                outermost = scope[outermost]
                            if isinstance(outermost, nodes.MapEntry) and outermost.map.schedule == dtypes.ScheduleType.GPU_Device:
                                child_in_gpu = True
                    _transify_in_sdfg(nd.sdfg, child_in_gpu)

    _transify_in_sdfg(sdfg)

    if verbose:
        print(f"  Transified {count} non-transient scalars in GPU kernels")


# ---------------------------------------------------------------------------
# Step 9: Handle dual-access arrays (interstate edges + GPU maps)
# ---------------------------------------------------------------------------

def _find_gpu_accessed_arrays(sdfg: dace.SDFG) -> Set[str]:
    """Find all array names accessed by GPU map scopes (top-level SDFG).

    Includes both arrays inside map scopes AND arrays connected to
    MapEntry inputs / MapExit outputs (which are at scope=None but
    participate in GPU data flow).
    """
    gpu_accessed = set()
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if not isinstance(node, nodes.MapEntry):
                continue
            if scope[node] is not None:
                continue
            if node.map.schedule != dtypes.ScheduleType.GPU_Device:
                continue

            map_exit = state.exit_node(node)

            # Arrays inside the map scope
            for n in state.all_nodes_between(node, map_exit):
                if isinstance(n, nodes.AccessNode) and n.data in sdfg.arrays:
                    gpu_accessed.add(n.data)

            # Arrays feeding into the map (MapEntry inputs)
            for e in state.in_edges(node):
                if isinstance(e.src, nodes.AccessNode) and e.src.data in sdfg.arrays:
                    gpu_accessed.add(e.src.data)

            # Arrays receiving from the map (MapExit outputs)
            for e in state.out_edges(map_exit):
                if isinstance(e.dst, nodes.AccessNode) and e.dst.data in sdfg.arrays:
                    gpu_accessed.add(e.dst.data)
    return gpu_accessed


def _handle_dual_access_arrays(sdfg: dace.SDFG, interstate: Set[str], verbose: bool):
    """Handle arrays that are on interstate edges AND inside GPU maps.

    For these arrays we create gpu_ duplicates (like non-transients), add
    H2D copies in the existing copy_in state, and rename references inside
    GPU maps.  Interstate edges keep the CPU name.

    For arrays that are also written inside GPU maps, we add D2H copy-backs.
    """
    gpu_accessed = _find_gpu_accessed_arrays(sdfg)
    dual = interstate & gpu_accessed

    # Find which are written inside GPU maps
    written_in_gpu = set()
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if not isinstance(node, nodes.AccessNode) or node.data not in dual:
                continue
            if not any(not e.data.is_empty() for e in state.in_edges(node)):
                continue
            parent = scope[node]
            if parent is None:
                continue
            outermost = parent
            while scope[outermost] is not None:
                outermost = scope[outermost]
            if isinstance(outermost, nodes.MapEntry) and outermost.map.schedule == dtypes.ScheduleType.GPU_Device:
                written_in_gpu.add(node.data)

    # Filter 'dual' to only include Arrays or written Scalars.
    # Read-only scalars are passed by value and don't need a gpu_ sibling.
    dual = {
        name for name in dual
        if isinstance(sdfg.arrays[name], dace.data.Array) or name in written_in_gpu
    }

    if not dual:
        if verbose:
            print(f"  No dual-access (interstate + GPU) arrays")
        return

    # Create gpu_ descriptors for dual arrays
    for name in dual:
        gpu_name = f"gpu_{name}"
        if gpu_name in sdfg.arrays:
            continue
        desc = sdfg.arrays[name]
        gpu_desc = copy.deepcopy(desc)
        gpu_desc.transient = True
        gpu_desc.storage = dtypes.StorageType.GPU_Global
        sdfg.add_datadesc(gpu_name, gpu_desc)

    # Add H2D copies to existing copy_in state
    h2d_state = None
    for node in sdfg.nodes():
        if hasattr(node, 'label') and node.label == 'copy_in_h2d':
            h2d_state = node
            break
    if h2d_state is not None:
        for name in dual:
            gpu_name = f"gpu_{name}"
            gpu_desc = sdfg.arrays[gpu_name]
            src = h2d_state.add_access(name)
            dst = h2d_state.add_access(gpu_name)
            h2d_state.add_edge(src, None, dst, None,
                               dace.Memlet.from_array(name, gpu_desc))

    # Add D2H copies for written dual arrays
    if written_in_gpu:
        d2h_state = None
        for node in sdfg.nodes():
            if hasattr(node, 'label') and node.label == 'copy_out_d2h':
                d2h_state = node
                break
        if d2h_state is not None:
            for name in written_in_gpu:
                gpu_name = f"gpu_{name}"
                gpu_desc = sdfg.arrays[gpu_name]
                src = d2h_state.add_access(gpu_name)
                dst = d2h_state.add_access(name)
                d2h_state.add_edge(src, None, dst, None,
                                   dace.Memlet.from_array(gpu_name, gpu_desc))

    # Rename references inside GPU maps for these dual arrays
    gpu_arrays_dual = {name for name in dual if f"gpu_{name}" in sdfg.arrays}
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if not isinstance(node, nodes.MapEntry):
                continue
            if scope[node] is not None:
                continue
            if node.map.schedule != dtypes.ScheduleType.GPU_Device:
                continue

            map_exit = state.exit_node(node)
            inner_nodes = set(state.all_nodes_between(node, map_exit))
            inner_nodes.add(node)
            inner_nodes.add(map_exit)
            for e in state.in_edges(node):
                if isinstance(e.src, nodes.AccessNode):
                    inner_nodes.add(e.src)
            for e in state.out_edges(map_exit):
                if isinstance(e.dst, nodes.AccessNode):
                    inner_nodes.add(e.dst)

            inner_edges = set()
            for n in inner_nodes:
                for e in state.in_edges(n):
                    inner_edges.add(e)
                for e in state.out_edges(n):
                    inner_edges.add(e)

            _rename_edges(sdfg, state, inner_edges, gpu_arrays_dual)
            _rename_access_nodes(inner_nodes, gpu_arrays_dual)
            _rename_nsdfg_connectors(sdfg, state, inner_nodes, gpu_arrays_dual)

    # Set gpu_ dual arrays storage to GPU_Global (they're already created as such)
    # Also set the original dual arrays that are inside GPU maps to GPU_Global
    # (they've been renamed to gpu_ now, so this is for any stragglers)

    if verbose:
        print(f"  Dual-access arrays: {dual}")
        if written_in_gpu:
            print(f"  Written in GPU (need D2H): {written_in_gpu}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def gpu_offload(sdfg: dace.SDFG, verbose: bool = False):
    """Apply GPU offloading transforms to the SDFG."""
    print("GPU offloading...")

    # 1. Classify
    non_trans, written, trans, interstate = _classify_arrays(sdfg, verbose)

    # 2. Create GPU descriptors for non-transient arrays
    _create_gpu_descriptors(sdfg, non_trans, verbose)

    # 3. H2D copy-in state
    _add_h2d_state(sdfg, non_trans)

    # 4. D2H copy-out state
    _add_d2h_state(sdfg, written)

    # 5. GPU schedules
    _set_gpu_schedules(sdfg, verbose)

    # 6. Rename non-transient references inside GPU map scopes
    gpu_arrays = _get_gpu_rename_set(sdfg)
    _rename_in_gpu_maps(sdfg, gpu_arrays, verbose)

    # 7. Handle dual-access arrays (interstate + GPU) — create gpu_ copies,
    #    add H2D/D2H, rename inside maps. Must come after step 6.
    _handle_dual_access_arrays(sdfg, interstate, verbose)

    # 8. Move transient arrays to GPU storage
    _set_transient_storage(sdfg, verbose)

    # 9. Transify non-transient scalars written inside GPU kernels
    _transify_gpu_scalars(sdfg, verbose)

    # 10. Propagate missing symbols and data through the hierarchy
    # (Fixes bugs where DaCe creates new symbols for ranges but doesn't link them)
    from utils.add_missing_symbols import add_missing_data_and_symbols_to_all_nsdfgs
    add_missing_data_and_symbols_to_all_nsdfgs(sdfg)

    # Re-index CFG
    sdfg.reset_cfg_list()
    print("GPU offloading complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GPU offload for CloudSC SDFG")
    parser.add_argument("--sdfg", type=str, required=True, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default="cloudsc_gpu.sdfgz")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print(f"Input:  {args.sdfg}")
    print(f"Output: {args.output}")
    sdfg = dace.SDFG.from_file(args.sdfg)

    gpu_offload(sdfg, verbose=args.verbose)

    sdfg.save(args.output, compress=True)
    print(f"Saved to {args.output}")
    sdfg.validate()
