"""gpu_offload.py — Transform a CPU-optimized SDFG for GPU execution.

Bespoke 9-step pipeline:
  1. Classify arrays (host-accessed / gpu-accessed / dual).
  2. Create gpu_<name> siblings for non-transients and dual-access transients.
  3. Set GPU schedule on outermost maps that touch gpu-relevant arrays.
  4. Add one H2D copy-in state.
  5. Rename references inside GPU map scopes to use gpu_<name>.
  6. Wrap loose host tasklets writing dual-access arrays as size-1 GPU maps
     (in their original position — preserves ordering of writes).
  7. Add one D2H copy-out state.
  8. Flip remaining transient arrays to GPU_Global storage.
  9. Transify non-transient scalars written inside GPU kernels.
 10. Propagate missing data/symbols through NestedSDFGs.

Checkpoints are always on and go to out_dir (default build/sdfgz/), matching
optimize.py's convention. No __main__ — invoke via cloudsc_gpu_pipeline.py.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Set

import dace
from dace import dtypes, nodes


def checkpoint(sdfg: dace.SDFG, name: str, out_dir: str):
    """Save a checkpoint SDFG into out_dir. Mirrors optimize.py's helper."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.sdfgz")
    sdfg.save(path, compress=True)
    print(f"  checkpoint: {path}")


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

    non_trans_arrays = []  # Needs gpu_ sibling and H2D/D2H
    trans_arrays = []      # Change storage directly to GPU_Global

    for name, desc in sdfg.arrays.items():
        if isinstance(desc, dace.data.Scalar) and name not in written:
            continue  # Read-only scalars are passed by value

        if not isinstance(desc, dace.data.Array) and not isinstance(desc, dace.data.Scalar):
            continue

        if not desc.transient:
            # Globals always get gpu_ siblings
            non_trans_arrays.append(name)
        else:
            # Transients
            if name in dual_access:
                # If accessed on BOTH host and GPU, treat it like a
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

    actual_copied = {e.src.data for e in h2d_state.edges() if isinstance(e.src, nodes.AccessNode)}
    for name in to_copy:
        assert name in actual_copied, f"Array {name} was not added to H2D copy state"

    print(f"  H2D state: {len(to_copy)} arrays")


def _add_d2h_state(sdfg: dace.SDFG, written_arrays: list[str]):
    """Add a sink state that copies written arrays back from GPU."""
    gpu_written = set()
    for sd in sdfg.all_sdfgs_recursive():
        for state in sd.states():
            for node in state.nodes():
                if isinstance(node, nodes.AccessNode) and node.data.startswith("gpu_"):
                    if state.in_degree(node) > 0:
                        orig = node.data[4:]
                        if orig in sdfg.arrays:
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

            _rename_edges(sdfg, state, inner_edges, gpu_arrays)
            _rename_access_nodes(inner_nodes, gpu_arrays)
            _rename_nsdfg_connectors(sdfg, state, inner_nodes, gpu_arrays)

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
# Step 6b: Wrap loose host tasklets writing dual-access arrays into size-1
#          GPU maps (in place). Preserves relative ordering of writes.
# ---------------------------------------------------------------------------

def _wrap_loose_host_tasklets_on_gpu(sdfg: dace.SDFG, dual_names: set, verbose: bool):
    """Wrap each loose host tasklet whose output AccessNode targets a
    dual-access array in a size-1 GPU map, in its original state and position.
    """
    from dace.sdfg.state import SDFGState
    wrapped_total = 0

    for sd in sdfg.all_sdfgs_recursive():
        for state in [n for n in sd.nodes() if isinstance(n, SDFGState)]:
            scope = state.scope_dict()
            loose_tasklets = []
            for nd in list(state.nodes()):
                if not isinstance(nd, nodes.Tasklet):
                    continue
                if scope[nd] is not None:
                    continue
                writes_dual = False
                for e in state.out_edges(nd):
                    if isinstance(e.dst, nodes.AccessNode) and e.dst.data in dual_names:
                        writes_dual = True
                        break
                if writes_dual:
                    loose_tasklets.append(nd)

            for gcode in loose_tasklets:
                for e in list(state.out_edges(gcode)):
                    if not isinstance(e.dst, nodes.AccessNode):
                        continue
                    dname = e.dst.data
                    if dname in dual_names:
                        host = dname
                        gpu_name = f"gpu_{host}"
                        if gpu_name not in sdfg.arrays:
                            continue
                        e.dst.data = gpu_name
                    elif dname.startswith("gpu_") and dname[4:] in dual_names:
                        host = dname[4:]
                        gpu_name = dname
                    else:
                        continue
                    if e.data is not None and e.data.data == host:
                        e.data.data = gpu_name

                me, mx = state.add_map(
                    gcode.label + '_gmap',
                    {gcode.label + '__gmapi': '0:1'},
                    schedule=dtypes.ScheduleType.GPU_Device,
                )
                in_edges = list(state.in_edges(gcode))
                out_edges = list(state.out_edges(gcode))
                me.in_connectors = {('IN_' + e.dst_conn): None for e in in_edges if e.dst_conn}
                me.out_connectors = {('OUT_' + e.dst_conn): None for e in in_edges if e.dst_conn}
                mx.in_connectors = {('IN_' + e.src_conn): None for e in out_edges if e.src_conn}
                mx.out_connectors = {('OUT_' + e.src_conn): None for e in out_edges if e.src_conn}

                for e in in_edges:
                    state.remove_edge(e)
                    state.add_edge(e.src, e.src_conn, me, 'IN_' + e.dst_conn, e.data)
                    state.add_edge(me, 'OUT_' + e.dst_conn, e.dst, e.dst_conn, copy.deepcopy(e.data))
                for e in out_edges:
                    state.remove_edge(e)
                    state.add_edge(e.src, e.src_conn, mx, 'IN_' + e.src_conn, e.data)
                    state.add_edge(mx, 'OUT_' + e.src_conn, e.dst, e.dst_conn, copy.deepcopy(e.data))

                if len(in_edges) == 0:
                    state.add_nedge(me, gcode, dace.Memlet())

                wrapped_total += 1

    if verbose or wrapped_total:
        print(f"  Wrapped {wrapped_total} loose host tasklets as size-1 GPU maps")
    return wrapped_total


# ---------------------------------------------------------------------------
# Step 6b: H2D before GPU reads of dual-access arrays that were written
#          on the host side (symmetric to step 6c below).
# ---------------------------------------------------------------------------

def _add_h2d_after_host_writes(sdfg: dace.SDFG, verbose: bool) -> int:
    """For each dual-access array X (has both X and gpu_X descriptors),
    if a host-side write to X has occurred since the last H2D, and we're
    about to enter a state where gpu_X is READ, splice an H2D state
    (X -> gpu_X) before the read state.

    Two-pass: analysis collects (cfr, edge, refs); mutation splices.
    """
    from dace.sdfg.state import (
        SDFGState, LoopRegion, ConditionalBlock,
        ControlFlowRegion, AbstractControlFlowRegion,
    )
    from dace.sdfg.analysis import cfg as cfg_analysis

    dual_names = {nm for nm in sdfg.arrays
                  if not nm.startswith("gpu_") and f"gpu_{nm}" in sdfg.arrays}
    if not dual_names:
        if verbose:
            print("  H2D after host writes: no dual-access arrays")
        return 0

    def _block_host_writes(blk, duals):
        if isinstance(blk, SDFGState):
            return _state_writes_host_dual(blk, duals)
        written = set()
        if isinstance(blk, (AbstractControlFlowRegion, ControlFlowRegion,
                            LoopRegion, ConditionalBlock)):
            for sub in blk.all_states():
                written |= _state_writes_host_dual(sub, duals)
        return written

    def _block_gpu_reads(blk, duals):
        if isinstance(blk, SDFGState):
            return _state_reads_gpu_dual(blk, duals)
        reads = set()
        if isinstance(blk, (AbstractControlFlowRegion, ControlFlowRegion,
                            LoopRegion, ConditionalBlock)):
            for sub in blk.all_states():
                reads |= _state_reads_gpu_dual(sub, duals)
        return reads

    def _splice_before(parent, blk, refs, sd, counter):
        """Splice an H2D state on every incoming edge of `blk`."""
        parent.add_state_before(blk, f"h2d_refresh_{blk.label[:24]}_{counter}",
                                is_start_block=False)

    n_added = 0

    for sd in sdfg.all_sdfgs_recursive():
        local_duals = {h for h in dual_names
                       if h in sd.arrays and f"gpu_{h}" in sd.arrays}
        if not local_duals:
            continue

        # dirty[X] = True means host X has newer data than gpu_X.
        dirty = {nm: False for nm in local_duals}
        # Collect (cfr, block, refs) to insert an H2D before that block.
        to_splice = []

        def _scan(cfr):
            if isinstance(cfr, ConditionalBlock):
                for _cond, branch in cfr.branches:
                    _scan(branch)
                return
            topo = list(cfg_analysis.blockorder_topological_sort(
                cfr, recursive=False, ignore_nonstate_blocks=False))
            for blk in topo:
                # Check if this block reads gpu_X with dirty host.
                reads = _block_gpu_reads(blk, local_duals)
                needed = {nm for nm in reads if dirty.get(nm, False)}
                if needed:
                    to_splice.append((cfr, blk, needed))
                    for nm in needed:
                        dirty[nm] = False
                # Then apply writes in this block.
                if isinstance(blk, SDFGState):
                    for nm in _state_writes_host_dual(blk, local_duals):
                        dirty[nm] = True
                else:
                    for nm in _block_host_writes(blk, local_duals):
                        dirty[nm] = True
                    _scan(blk)

        _scan(sd)

        for cfr, blk, refs in to_splice:
            # Insert an H2D state BEFORE blk in cfr.
            desc_map = {nm: sd.arrays[nm] for nm in refs}
            new_state = cfr.add_state_before(
                blk, f"h2d_refresh_{blk.label[:24]}_{n_added}",
                is_start_block=False,
            )
            for host_nm, desc in desc_map.items():
                gpu_nm = f"gpu_{host_nm}"
                src_an = new_state.add_access(host_nm)
                dst_an = new_state.add_access(gpu_nm)
                new_state.add_edge(src_an, None, dst_an, None,
                                   dace.Memlet.from_array(host_nm, desc))
            n_added += 1

    if verbose or n_added:
        print(f"  H2D after host writes: {n_added} states inserted")
    return n_added


# ---------------------------------------------------------------------------
# Step 6c: D2H before interstate host reads of dual-access arrays
# ---------------------------------------------------------------------------

def _state_writes_gpu_dual(state, dual_names):
    """Return the subset of dual_names whose gpu_<name> sibling is written
    (directly as an AccessNode-in-edge, or via an NSDFG out-connector) in
    this state."""
    written = set()
    for nd in state.nodes():
        if isinstance(nd, nodes.AccessNode) and nd.data.startswith("gpu_"):
            host_nm = nd.data[len("gpu_"):]
            if host_nm in dual_names and state.in_degree(nd) > 0:
                written.add(host_nm)
        if isinstance(nd, nodes.NestedSDFG):
            for conn in nd.out_connectors:
                if conn.startswith("gpu_"):
                    host_nm = conn[len("gpu_"):]
                    if host_nm in dual_names:
                        written.add(host_nm)
    return written


def _state_writes_host_dual(state, dual_names):
    """Names in dual_names whose HOST copy X is written on CPU in this state
    (AccessNode at scope=None with in-edges)."""
    sdict = state.scope_dict()
    written = set()
    for nd in state.nodes():
        if isinstance(nd, nodes.AccessNode) and nd.data in dual_names:
            if sdict.get(nd) is None and state.in_degree(nd) > 0:
                written.add(nd.data)
    return written


def _state_reads_gpu_dual(state, dual_names):
    """Names whose gpu_<name> sibling is READ in this state (by a kernel
    or via an NSDFG in-connector)."""
    reads = set()
    for nd in state.nodes():
        if isinstance(nd, nodes.AccessNode) and nd.data.startswith("gpu_"):
            host_nm = nd.data[len("gpu_"):]
            if host_nm in dual_names and state.out_degree(nd) > 0:
                reads.add(host_nm)
        if isinstance(nd, nodes.NestedSDFG):
            for conn in nd.in_connectors:
                if conn.startswith("gpu_"):
                    host_nm = conn[len("gpu_"):]
                    if host_nm in dual_names:
                        reads.add(host_nm)
    return reads


def _add_d2h_before_interstate_reads(sdfg: dace.SDFG, verbose: bool) -> int:
    """Insert D2H refresh states before host-interstate reads of dual-access
    arrays, but ONLY when a GPU write to gpu_<name> has occurred since the
    last refresh. Topological walk per CFR with a per-dual "dirty" flag.

    Conservative for loops: if any state in a LoopRegion body writes gpu_<X>,
    the dirty flag for <X> is re-set on every back-edge (i.e. every iteration
    can cause a refresh on reads inside the body).
    """
    from dace.sdfg.state import SDFGState, LoopRegion, ConditionalBlock
    from dace.sdfg.state import ControlFlowRegion, AbstractControlFlowRegion
    from dace.sdfg.analysis import cfg as cfg_analysis

    dual_names = {nm for nm in sdfg.arrays
                  if not nm.startswith("gpu_") and f"gpu_{nm}" in sdfg.arrays}
    if not dual_names:
        if verbose:
            print("  D2H interstate refresh: no dual-access arrays")
        return 0

    n_added = 0

    def _interstate_refs(edge, duals):
        refs = set()
        sources = []
        if edge.data.condition is not None:
            sources.append(edge.data.condition.as_string)
        for v in edge.data.assignments.values():
            sources.append(str(v))
        joined = "\n".join(sources)
        for nm in duals:
            if re.search(r"\b" + re.escape(nm) + r"\b", joined):
                refs.add(nm)
        return refs

    def _block_writes(blk, duals):
        """Union of gpu_<X> writes across all states reachable inside `blk`."""
        written = set()
        if isinstance(blk, SDFGState):
            written |= _state_writes_gpu_dual(blk, duals)
        elif isinstance(blk, (AbstractControlFlowRegion, ControlFlowRegion,
                              LoopRegion, ConditionalBlock)):
            for sub in blk.all_states():
                written |= _state_writes_gpu_dual(sub, duals)
        return written

    def _splice(parent, e, refs, sd, counter):
        refresh_state = parent.add_state(
            f"refresh_host_{e.src.label[:24]}_{counter}"
        )
        for host_nm in refs:
            gpu_nm = f"gpu_{host_nm}"
            src_an = refresh_state.add_access(gpu_nm)
            dst_an = refresh_state.add_access(host_nm)
            desc = sd.arrays[host_nm]
            refresh_state.add_edge(src_an, None, dst_an, None,
                                   dace.Memlet.from_array(host_nm, desc))
        cond = e.data.condition
        asgn = dict(e.data.assignments)
        parent.remove_edge(e)
        parent.add_edge(e.src, refresh_state, dace.InterstateEdge(condition=cond))
        parent.add_edge(refresh_state, e.dst, dace.InterstateEdge(assignments=asgn))

    for sd in sdfg.all_sdfgs_recursive():
        local_duals = {h for h in dual_names
                       if h in sd.arrays and f"gpu_{h}" in sd.arrays}
        if not local_duals:
            continue

        # Pass 1: analysis. Walk CFRs recursively with a shared dirty dict
        # and collect (cfr, edge, refs) tuples that need refresh. No mutation.
        dirty = {nm: False for nm in local_duals}
        to_splice = []  # list of (cfr, edge, refs)

        def _scan(cfr):
            # ConditionalBlock doesn't have a single start_block / dominator
            # tree — its structure is (cond_i, branch_cfr_i) pairs. Iterate
            # branches directly; the pass only needs to visit inner writers
            # and inner out_edges, not dominator order at this level.
            if isinstance(cfr, ConditionalBlock):
                for _cond, branch in cfr.branches:
                    _scan(branch)
                return
            topo = list(cfg_analysis.blockorder_topological_sort(
                cfr, recursive=False, ignore_nonstate_blocks=False))
            for blk in topo:
                if isinstance(blk, SDFGState):
                    for nm in _state_writes_gpu_dual(blk, local_duals):
                        dirty[nm] = True
                else:
                    # Nested CFR. Pre-set dirty for arrays any descendant
                    # writes, so loop back-edges re-contaminate on iteration.
                    for nm in _block_writes(blk, local_duals):
                        dirty[nm] = True
                    _scan(blk)
                for e in cfr.out_edges(blk):
                    refs = _interstate_refs(e, local_duals)
                    needed = {nm for nm in refs if dirty.get(nm, False)}
                    if needed:
                        to_splice.append((cfr, e, needed))
                        for nm in needed:
                            dirty[nm] = False

        _scan(sd)

        # Pass 2: mutation. Splice the collected edges.
        for cfr, e, refs in to_splice:
            _splice(cfr, e, refs, sd, n_added)
            n_added += 1

    if verbose or n_added:
        print(f"  D2H interstate refresh: {n_added} states inserted")
    return n_added


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

    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, nodes.NestedSDFG):
                count += _set_nsdfg_gpu_storage(node.sdfg)

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

def gpu_offload(sdfg: dace.SDFG, verbose: bool = False, out_dir: str = "build/sdfgz"):
    """Apply GPU offloading transforms to the SDFG. Checkpoints after each
    step into `out_dir` (default build/sdfgz/), following optimize.py's
    convention."""
    print("GPU offloading...")
    checkpoint(sdfg, "gpu_entry", out_dir)

    non_trans, written, trans, interstate = _classify_arrays(sdfg, verbose)
    checkpoint(sdfg, "after_gpu_classify", out_dir)

    _create_gpu_descriptors(sdfg, non_trans, verbose)
    checkpoint(sdfg, "after_gpu_descriptors", out_dir)

    gpu_array_set = set(non_trans) | set(trans)
    _set_gpu_schedules(sdfg, gpu_array_set, verbose)
    checkpoint(sdfg, "after_gpu_schedules", out_dir)

    _add_h2d_state(sdfg, non_trans)
    checkpoint(sdfg, "after_gpu_h2d", out_dir)

    gpu_arrays = _get_gpu_rename_set(sdfg)
    _rename_in_gpu_maps(sdfg, gpu_arrays, verbose)
    checkpoint(sdfg, "after_gpu_rename", out_dir)

    from gpu_classifier import classify
    _classification = classify(sdfg)
    _dual_names = set()
    for _nm, _role in _classification.array_roles.items():
        if _nm.startswith("gpu_") and _role.is_dual:
            _host = _nm[len("gpu_"):]
            if _host in sdfg.arrays:
                _dual_names.add(_host)
        elif _role.is_dual and f"gpu_{_nm}" in sdfg.arrays:
            _dual_names.add(_nm)
    _wrap_loose_host_tasklets_on_gpu(sdfg, _dual_names, verbose)
    checkpoint(sdfg, "after_gpu_wrap_loose", out_dir)

    _add_h2d_after_host_writes(sdfg, verbose)
    checkpoint(sdfg, "after_gpu_h2d_refresh", out_dir)

    _add_d2h_before_interstate_reads(sdfg, verbose)
    checkpoint(sdfg, "after_gpu_interstate_refresh", out_dir)

    _add_d2h_state(sdfg, written)
    checkpoint(sdfg, "after_gpu_d2h", out_dir)

    _set_transient_storage(sdfg, trans, verbose)

    _transify_gpu_scalars(sdfg, verbose)

    from utils.add_missing_symbols import add_missing_data_and_symbols_to_all_nsdfgs
    add_missing_data_and_symbols_to_all_nsdfgs(sdfg)

    sdfg.reset_cfg_list()
    checkpoint(sdfg, "after_gpu_finalize", out_dir)
    print("GPU offloading complete.")
