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

def _find_host_accessed_arrays(sdfg: dace.SDFG) -> Set[str]:
    """Find all array names accessed at the host level (outside GPU maps)."""
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
    return host_accessed


def _collect_map_arrays(state, map_entry) -> Set[str]:
    """Collect all array names accessed by a single map scope (including boundary)."""
    sdfg = state.parent
    arrays = set()
    map_exit = state.exit_node(map_entry)
    for n in state.all_nodes_between(map_entry, map_exit):
        if isinstance(n, nodes.AccessNode) and n.data in sdfg.arrays:
            arrays.add(n.data)
    for e in state.in_edges(map_entry):
        if isinstance(e.src, nodes.AccessNode) and e.src.data in sdfg.arrays:
            arrays.add(e.src.data)
    for e in state.out_edges(map_exit):
        if isinstance(e.dst, nodes.AccessNode) and e.dst.data in sdfg.arrays:
            arrays.add(e.dst.data)
    return arrays


def _find_gpu_read_arrays(sdfg: dace.SDFG) -> Set[str]:
    """Find arrays that are READ inside top-level map scopes."""
    gpu_read = set()
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if not isinstance(node, nodes.MapEntry):
                continue
            if scope[node] is not None:
                continue
            # Inputs feeding into the map entry
            for e in state.in_edges(node):
                if isinstance(e.src, nodes.AccessNode) and e.src.data in sdfg.arrays:
                    gpu_read.add(e.src.data)
            # AccessNodes read inside the map (have outgoing edges)
            map_exit = state.exit_node(node)
            for n in state.all_nodes_between(node, map_exit):
                if isinstance(n, nodes.AccessNode) and n.data in sdfg.arrays:
                    if any(not e.data.is_empty() for e in state.out_edges(n)):
                        gpu_read.add(n.data)
    return gpu_read


def _find_gpu_accessed_arrays(sdfg: dace.SDFG) -> Set[str]:
    """Find arrays accessed by GPU-worthy maps.

    A map is GPU-worthy if it touches at least one array that is READ inside
    some top-level map.  Pure init maps (write-only to host-only arrays like
    llfall) are excluded, keeping those arrays CPU-only.
    """
    gpu_read = _find_gpu_read_arrays(sdfg)
    gpu_accessed = set()
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if not isinstance(node, nodes.MapEntry):
                continue
            if scope[node] is not None:
                continue
            map_arrays = _collect_map_arrays(state, node)
            # Only include if this map touches at least one gpu_read array
            if map_arrays & gpu_read:
                gpu_accessed |= map_arrays
    return gpu_accessed


def _classify_arrays(sdfg: dace.SDFG, verbose: bool):
    """Partition SDFG data into categories for GPU offloading."""
    
    # Identify which arrays are written anywhere
    written = set()
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nodes.AccessNode) and state.in_degree(node) > 0:
                data_name = node.data
                if data_name in sdfg.arrays:
                    # Only count as written if there is a real incoming edge (not empty)
                    if any(not e.data.is_empty() for e in state.in_edges(node)):
                        written.add(data_name)
            if isinstance(node, nodes.NestedSDFG):
                for conn, array_name in node.out_connectors.items():
                    if array_name in sdfg.arrays:
                        written.add(array_name)

    host_accessed = _find_host_accessed_arrays(sdfg)
    gpu_accessed = _find_gpu_accessed_arrays(sdfg)
    dual_access = host_accessed & gpu_accessed

    non_trans_arrays = [] # Needs gpu_ sibling and H2D/D2H
    trans_arrays = []     # Change storage directly to GPU_Global

    for name, desc in sdfg.arrays.items():
        if isinstance(desc, dace.data.Scalar) and name not in written:
            continue # Read-only scalars are passed by value
        
        if not isinstance(desc, dace.data.Array) and not isinstance(desc, dace.data.Scalar):
            continue

        if not desc.transient:
            # Globals always get gpu_ siblings
            non_trans_arrays.append(name)
        else:
            # Transients
            if name in dual_access:
                # If accessed on BOTH host and GPU, it's safer to treat it like a 
                # non-transient: keep the host one and create a gpu_ sibling.
                non_trans_arrays.append(name)
            elif name in gpu_accessed:
                # Only GPU: move storage directly
                trans_arrays.append(name)
            # Else: Only Host or Unused, leave as Default

    # Find arrays referenced on interstate edges (subset of host_accessed usually)
    interstate = set()
    for edge in sdfg.all_interstate_edges():
        for sym in edge.data.free_symbols:
            sym_str = str(sym)
            if sym_str in sdfg.arrays:
                interstate.add(sym_str)

    written_arrays = [n for n in non_trans_arrays if n in written]

    if verbose:
        print(f"  Non-transient/Dual-access arrays: {len(non_trans_arrays)}")
        print(f"  Written (need D2H): {len(written_arrays)}")
        print(f"  GPU-only Transients: {len(trans_arrays)}")
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

    for name in array_names:
        assert f"gpu_{name}" in sdfg.arrays, f"Failed to create GPU descriptor for {name}"

    if verbose:
        print(f"  Created {len(array_names)} GPU array descriptors")


# ---------------------------------------------------------------------------
# Step 3 & 4: H2D / D2H copy states
# ---------------------------------------------------------------------------

def _add_h2d_state(sdfg: dace.SDFG, array_names: list[str]):
    """Add a start state that copies non-transient arrays to GPU if used in GPU maps."""
    # We copy IF it is accessed by a GPU map AND is not a transient
    # (transients are computed inside the SDFG — copying before computation gives garbage)
    gpu_accessed = _find_gpu_accessed_arrays(sdfg)
    to_copy = [n for n in array_names
               if n in gpu_accessed and not sdfg.arrays[n].transient]

    prev_start = sdfg.start_block
    h2d_state = sdfg.add_state("copy_in_h2d")
    sdfg.start_block = sdfg.node_id(h2d_state)
    sdfg.add_edge(h2d_state, prev_start, dace.InterstateEdge())

    for name in to_copy:
        gpu_name = f"gpu_{name}"
        gpu_desc = sdfg.arrays[gpu_name]
        src = h2d_state.add_access(name)
        dst = h2d_state.add_access(gpu_name)
        h2d_state.add_edge(src, None, dst, None,
                           dace.Memlet.from_array(name, gpu_desc))

    # Verification: Every array in to_copy should have an outgoing edge in h2d_state
    actual_copied = {e.src.data for e in h2d_state.edges() if isinstance(e.src, nodes.AccessNode)}
    for name in to_copy:
        assert name in actual_copied, f"Array {name} was not added to H2D copy state"

    print(f"  H2D state: {len(to_copy)} arrays")


def _add_d2h_state(sdfg: dace.SDFG, written_arrays: list[str]):
    """Add a sink state that copies written arrays back from GPU."""
    # Find which ones are actually written on GPU (subset of written_arrays)
    # We look for AccessNodes named gpu_{name} with in-degree > 0
    gpu_written = set()
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nodes.AccessNode) and node.data.startswith("gpu_"):
                if state.in_degree(node) > 0:
                    orig = node.data[4:]
                    if orig in written_arrays:
                        gpu_written.add(orig)

    sinks = sdfg.sink_nodes()
    d2h_state = sdfg.add_state("copy_out_d2h")

    for sink in sinks:
        sdfg.add_edge(sink, d2h_state, dace.InterstateEdge())

    for name in gpu_written:
        gpu_name = f"gpu_{name}"
        gpu_desc = sdfg.arrays[gpu_name]
        src = d2h_state.add_access(gpu_name)
        dst = d2h_state.add_access(name)
        d2h_state.add_edge(src, None, dst, None,
                           dace.Memlet.from_array(gpu_name, gpu_desc))

    # Verification
    actual_copied = {e.dst.data for e in d2h_state.edges() if isinstance(e.dst, nodes.AccessNode)}
    for name in gpu_written:
        assert name in actual_copied, f"Array {name} was not added to D2H copy state"

    print(f"  D2H state: {len(gpu_written)} arrays")


# ---------------------------------------------------------------------------
# Step 5: Set GPU schedules
# ---------------------------------------------------------------------------

def _set_gpu_schedules(sdfg: dace.SDFG, gpu_arrays: Set[str], verbose: bool):
    """Set outermost Maps to GPU_Device schedule if they touch GPU arrays."""
    count = 0
    skipped = []
    for state in sdfg.all_states():
        scope = state.scope_dict()
        for node in state.nodes():
            if not isinstance(node, nodes.MapEntry) or scope[node] is not None:
                continue
            map_arrays = _collect_map_arrays(state, node)
            if map_arrays & gpu_arrays:
                node.map.schedule = dtypes.ScheduleType.GPU_Device
                count += 1
            else:
                skipped.append(node.map.label)
    if skipped:
        print(f"  Kept {len(skipped)} maps on CPU (no GPU arrays): {skipped}")
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

            # Verification: Check no stragglers
            for n in inner_nodes:
                if isinstance(n, nodes.AccessNode) and n.data in gpu_arrays:
                    raise AssertionError(f"AccessNode for {n.data} was not renamed in GPU map {node.map.label}")

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

    # Rename on interstate edges
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
# Step 6b: Redirect loose tasklet writes of dual-access transients
# ---------------------------------------------------------------------------

def _is_map_boundary(state, nd):
    """Check if an AccessNode is a boundary node feeding into/out of a map."""
    for e in state.out_edges(nd):
        if isinstance(e.dst, nodes.MapEntry):
            return True
    for e in state.in_edges(nd):
        if isinstance(e.src, nodes.MapExit):
            return True
    return False


def _find_loose_nodes(state):
    """Find all nodes at top scope that are NOT part of any map scope."""
    scope = state.scope_dict()
    loose = set()
    for nd in state.nodes():
        if scope[nd] is not None:
            continue  # Inside a map
        if isinstance(nd, (nodes.MapEntry, nodes.MapExit)):
            continue
        if isinstance(nd, nodes.AccessNode) and _is_map_boundary(state, nd):
            continue
        loose.add(nd)
    return loose


def _extract_loose_into(state, target_state):
    """Move all loose nodes (and their inter-edges) from state into target_state.

    Returns the set of moved nodes, or empty set if nothing moved.
    """
    loose = _find_loose_nodes(state)
    if not loose:
        return set()

    # Collect edges between loose nodes before removing anything
    loose_edges = []
    for e in state.edges():
        if e.src in loose and e.dst in loose:
            loose_edges.append((e.src, e.src_conn, e.dst, e.dst_conn,
                                copy.deepcopy(e.data)))

    # Add to target
    for nd in loose:
        target_state.add_node(nd)
    for src, sc, dst, dc, data in loose_edges:
        target_state.add_edge(src, sc, dst, dc, data)

    # Remove from original
    for nd in loose:
        state.remove_node(nd)

    return loose


def _add_deferred_h2d_copies(sdfg: dace.SDFG, gpu_arrays: set, verbose: bool):
    """Float ALL host-side scalar/array computations into ONE dedicated
    precompute state at the very beginning, then add a single H2D copy state.

    1. Create one precompute state.
    2. For every top-level state, extract loose tasklets into the precompute
       state (removing them from their original mixed states).
    3. Place precompute right after copy_in_h2d.
    4. Add a consolidated H2D copy state after precompute.
    5. For writes inside nested CFGs (ConditionalBlocks, LoopRegions),
       add per-state H2D copies.

    Only processes TRANSIENT arrays — non-transients are handled by the
    initial H2D + D2H states.
    """
    # --- Part A: Consolidate all loose tasklets into one precompute state ---
    all_deferred_top = set()
    # Create a standalone state (not yet wired into the SDFG)
    from dace.sdfg.state import SDFGState
    precompute = SDFGState("host_precompute", sdfg)
    moved_any = False

    # Only process DIRECT children of the top-level SDFG (not inside
    # ConditionalBlocks or LoopRegions — those are handled by Part C).
    for state in [n for n in sdfg.nodes() if isinstance(n, SDFGState)]:
        scope_fn = getattr(state, 'scope_dict', None)
        if scope_fn is None:
            continue
        scope = scope_fn()

        # Check if this state has loose writes to dual-access transients
        has_deferred = False
        for nd in state.nodes():
            if not isinstance(nd, nodes.AccessNode):
                continue
            if nd.data not in gpu_arrays:
                continue
            if nd.data not in sdfg.arrays or not sdfg.arrays[nd.data].transient:
                continue
            if scope[nd] is not None:
                continue
            if _is_map_boundary(state, nd):
                continue
            if any(not e.data.is_empty() for e in state.in_edges(nd)):
                all_deferred_top.add(nd.data)
                has_deferred = True

        if not has_deferred:
            continue

        # Extract loose nodes from this state into the precompute state
        moved = _extract_loose_into(state, precompute)
        if moved:
            moved_any = True

    if not moved_any:
        # Nothing was extracted — discard the standalone state
        pass  # precompute was never added to the SDFG
    else:
        # Add precompute BEFORE copy_in_h2d (precompute becomes new start)
        sdfg.add_node(precompute)
        h2d_state = sdfg.start_block  # copy_in_h2d
        sdfg.add_edge(precompute, h2d_state, dace.InterstateEdge())
        sdfg.start_block = sdfg.node_id(precompute)

    # --- Part B: Merge scalar H2D copies into the existing copy_in_h2d ---
    if all_deferred_top and moved_any:
        h2d_state = [s for s in sdfg.states() if s.label == "copy_in_h2d"][0]
        for name in sorted(all_deferred_top):
            gpu_name = f"gpu_{name}"
            if gpu_name not in sdfg.arrays:
                continue
            desc = sdfg.arrays[gpu_name]
            src = h2d_state.add_access(name)
            dst = h2d_state.add_access(gpu_name)
            h2d_state.add_edge(src, None, dst, None,
                               dace.Memlet.from_array(name, desc))

    # --- Part C: Per-state H2D for writes inside nested CFGs ---
    nested_count = 0
    for sd in sdfg.all_sdfgs_recursive():
        for cfg in sd.all_control_flow_regions():
            if cfg is sdfg:
                continue  # Top-level handled above
            for state in cfg.nodes():
                if not hasattr(state, 'scope_dict'):
                    continue
                scope = state.scope_dict()
                written = set()
                for nd in state.nodes():
                    if not (isinstance(nd, nodes.AccessNode) and nd.data in gpu_arrays):
                        continue
                    if not sdfg.arrays[nd.data].transient:
                        continue
                    if scope[nd] is None and any(
                            not e.data.is_empty() for e in state.in_edges(nd)):
                        written.add(nd.data)
                if written:
                    h2d = cfg.add_state(f"{state.label}_h2d")
                    for name in written:
                        gpu_name = f"gpu_{name}"
                        if gpu_name not in sdfg.arrays:
                            continue
                        desc = sdfg.arrays[gpu_name]
                        src = h2d.add_access(name)
                        dst = h2d.add_access(gpu_name)
                        h2d.add_edge(src, None, dst, None,
                                     dace.Memlet.from_array(name, desc))
                        nested_count += 1
                    for out_edge in list(cfg.out_edges(state)):
                        cfg.remove_edge(out_edge)
                        cfg.add_edge(h2d, out_edge.dst, out_edge.data)
                    cfg.add_edge(state, h2d, dace.InterstateEdge())

    if verbose or moved_any or all_deferred_top or nested_count:
        if moved_any:
            print(f"  Floated all host computes into 1 precompute state")
        print(f"  Consolidated H2D: {len(all_deferred_top)} top-level transients")
        if nested_count:
            print(f"  Nested H2D: {nested_count} copies in nested CFGs")


# ---------------------------------------------------------------------------
# Step 7: Set GPU storage for transient arrays
# ---------------------------------------------------------------------------

def _set_transient_storage(sdfg: dace.SDFG, trans_arrays: list[str], verbose: bool):
    """Move GPU-only transient Arrays to GPU_Global storage."""
    count = 0
    for name in trans_arrays:
        desc = sdfg.arrays[name]
        if isinstance(desc, dace.data.Array):
            desc.storage = dtypes.StorageType.GPU_Global
            count += 1
        elif isinstance(desc, dace.data.Scalar):
            desc.storage = dtypes.StorageType.Register
            count += 1

    # Recurse into NestedSDFGs
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nodes.NestedSDFG):
                count += _set_nsdfg_gpu_storage(node.sdfg)

    # Verification: No transient should be CPU_Pinned (illegal access cause)
    for name, desc in sdfg.arrays.items():
        if desc.transient:
            assert desc.storage != dtypes.StorageType.CPU_Pinned, f"Transient {name} was promoted to CPU_Pinned"

    if verbose:
        print(f"  Moved {count} transient descriptors to GPU storage")


def _set_nsdfg_gpu_storage(nsdfg: dace.SDFG) -> int:
    """Recursively set all descriptors inside a GPU-nested SDFG to GPU storage."""
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
    """Convert non-transient scalars written inside GPU maps to transient Register."""
    count = 0

    def _transify_in_sdfg(current_sdfg, in_gpu_scope=False):
        nonlocal count
        for name, desc in list(current_sdfg.arrays.items()):
            if not isinstance(desc, dace.data.Scalar):
                continue
            if desc.transient:
                continue
            
            written_in_gpu = False
            for st in current_sdfg.all_states():
                scope = st.scope_dict()
                for nd in st.nodes():
                    if isinstance(nd, nodes.AccessNode) and nd.data == name:
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
                if f"gpu_{name}" not in sdfg.arrays:
                    desc.transient = True
                    desc.storage = dtypes.StorageType.Register
                    count += 1

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
# Main pipeline
# ---------------------------------------------------------------------------

def gpu_offload(sdfg: dace.SDFG, verbose: bool = False):
    """Apply GPU offloading transforms to the SDFG."""
    print("GPU offloading...")

    # 1. Classify
    non_trans, written, trans, interstate = _classify_arrays(sdfg, verbose)

    # 2. Create GPU descriptors for non-transient and dual-access arrays
    _create_gpu_descriptors(sdfg, non_trans, verbose)

    # 3. GPU schedules — only for maps that touch GPU arrays
    gpu_array_set = set(non_trans) | set(trans)
    _set_gpu_schedules(sdfg, gpu_array_set, verbose)

    # 4. H2D copy-in state
    _add_h2d_state(sdfg, non_trans)

    # 5. D2H copy-out state
    _add_d2h_state(sdfg, written)

    # 6. Rename non-transient references inside GPU map scopes
    gpu_arrays = _get_gpu_rename_set(sdfg)
    _rename_in_gpu_maps(sdfg, gpu_arrays, verbose)

    # 6b. Add deferred H2D copies for transients computed on host
    _add_deferred_h2d_copies(sdfg, gpu_arrays, verbose)

    # 7. Move transient arrays to GPU storage
    _set_transient_storage(sdfg, trans, verbose)

    # 8. Transify non-transient scalars written inside GPU kernels
    _transify_gpu_scalars(sdfg, verbose)

    # 9. Propagate missing symbols and data through the hierarchy
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
