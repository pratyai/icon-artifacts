"""unroll.py — Propagate constant symbols and unroll matching maps/loops.

Two public functions:

- ``propagate_constants(sdfg, symbol_map)`` — replace symbols with concrete
  values while preserving the external interface (non-transient array shapes
  and SDFG symbols).

- ``unroll_loops(sdfg, symbol_map)`` — find maps/loops whose extent matches
  a propagated value and unroll them one at a time.

The legacy ``unroll(sdfg, symbol_map)`` combines both steps.

Usage (standalone):
    python -m ssa.unroll input.sdfgz -o output.sdfgz --symbols nclv=5
"""

from __future__ import annotations

import copy
from typing import Dict

import dace
from tqdm import tqdm
from dace import nodes as nd
from dace.sdfg.state import LoopRegion
from dace.transformation.dataflow import MapUnroll
from dace.transformation.interstate import LoopUnroll
from dace.transformation.passes.analysis import loop_analysis


def propagate_constants(sdfg: dace.SDFG, symbol_map: Dict[str, int]):
    """Replace symbols with concrete values, preserving the external interface.

    Non-transient array shapes are snapshotted before replacement and restored
    afterwards.  Symbols still referenced by the interface are re-added.
    """
    # Snapshot original shapes for non-transient (interface) arrays
    orig_shapes: dict[str, list] = {}
    for name, desc in sdfg.arrays.items():
        if not desc.transient:
            orig_shapes[name] = copy.deepcopy(list(desc.shape))

    sdfg.replace_dict(symbol_map)

    # Restore original interface shapes
    for name, shape in orig_shapes.items():
        if name in sdfg.arrays:
            sdfg.arrays[name].shape = shape

    # Re-add propagated symbols and make them "free" so they appear in
    # __dace_init.  We create a transient array with a symbolic shape and
    # an AccessNode in the start state — this survives simplify.
    start_state = sdfg.start_block
    for sym_name in symbol_map:
        if sym_name not in sdfg.symbols:
            sdfg.add_symbol(sym_name, dace.int32)
        dummy = f"__keep_{sym_name}"
        if dummy not in sdfg.arrays:
            sym = dace.symbolic.pystr_to_symbolic(sym_name)
            sdfg.add_array(dummy, [sym], dace.int32, transient=True)
            start_state.add_access(dummy)

    print(f"Propagate constants: {symbol_map}")


def unroll_loops(sdfg: dace.SDFG, symbol_map: Dict[str, int]):
    """Unroll maps and loops whose iteration count matches a propagated value.

    Each unroll mutates the graph and invalidates node references, so we
    find-and-unroll one target at a time.
    """
    import time
    potential_ranges = set(symbol_map.values())

    unrolled_maps = 0
    unrolled_loops = 0
    skipped = 0
    round_num = 0
    pbar = tqdm(desc="Unrolling", unit="op")

    while True:
        # Collect all current targets in one pass
        t0 = time.time()
        batch = _collect_unroll_targets(sdfg, potential_ranges)
        t_collect = time.time() - t0
        if not batch:
            break
        round_num += 1
        pbar.total = (pbar.n or 0) + len(batch)
        pbar.refresh()
        pbar.write(f"  Round {round_num}: {len(batch)} targets "
                   f"(collect: {t_collect:.1f}s)")
        for kind, node, parent in batch:
            if kind == "map":
                label = node.map.label
                ranges = str(node.map.range)
            else:
                label = node.label
                ranges = f"{node.loop_variable}"
            pbar.write(f"    {kind}: {label} [{ranges}]")

        applied_any = False
        for kind, node, parent in batch:
            label = node.map.label if kind == "map" else node.label
            pbar.set_postfix_str(f"{kind}: {label}")
            t0 = time.time()
            try:
                if kind == "map":
                    # Direct apply — bypass apply_to's slow state search
                    _apply_map_unroll(parent, node)
                    unrolled_maps += 1
                else:
                    owner = parent
                    while not isinstance(owner, dace.SDFG):
                        owner = owner.parent_graph
                    LoopUnroll().apply_to(
                        sdfg=owner, options={"inline_iterations": True},
                        loop=node,
                    )
                    unrolled_loops += 1
                applied_any = True
                dt = time.time() - t0
                if dt > 2.0:
                    pbar.write(f"    slow: {kind} {label} took {dt:.1f}s")
            except Exception:
                skipped += 1
                continue
            pbar.update(1)

        if not applied_any:
            break  # all remaining targets failed

    pbar.close()

    print(f"Unroll: {unrolled_maps} maps, {unrolled_loops} loops"
          f"{f', {skipped} skipped' if skipped else ''}")


def _apply_map_unroll(state, map_entry):
    """Apply MapUnroll directly, skipping apply_to's O(N) state search."""
    owner_sdfg = state.parent
    cfg_id = state.parent_graph.cfg_id
    state_id = state.block_id
    node_id = state.node_id(map_entry)

    instance = MapUnroll()
    instance.setup_match(
        owner_sdfg, cfg_id, state_id,
        {MapUnroll.map_entry: node_id}, 0,
    )
    if not instance.can_be_applied(state, 0, owner_sdfg):
        raise ValueError("MapUnroll not applicable")
    instance.apply_pattern(annotate=True, append=False)


def unroll(sdfg: dace.SDFG, symbol_map: Dict[str, int]):
    """Propagate constants then unroll (legacy combined interface)."""
    propagate_constants(sdfg, symbol_map)
    unroll_loops(sdfg, symbol_map)


# ── Internal helpers ──────────────────────────────────────────────────────


def _collect_unroll_targets(sdfg, potential_ranges):
    """Collect all maps and loops whose extent matches a target range.

    Returns a list of (kind, node, parent) tuples.  Maps are returned first
    (cheaper to unroll, less graph bloat).
    """
    maps = []
    loops = []

    for n, g in sdfg.all_nodes_recursive():
        if isinstance(n, nd.MapEntry):
            for _, r in zip(n.map.params, n.map.range):
                extent = ((r[1] + 1) - r[0]) // r[2]
                if extent.free_symbols:
                    continue
                try:
                    val = int(extent)
                except (TypeError, ValueError):
                    continue
                if val in potential_ranges:
                    maps.append(("map", n, g))
                    break

    for cfg in sdfg.all_control_flow_regions():
        for child in cfg.nodes():
            if not isinstance(child, LoopRegion):
                continue
            beg = loop_analysis.get_init_assignment(child)
            end = loop_analysis.get_loop_end(child)
            step = loop_analysis.get_loop_stride(child)
            if beg is None or end is None or step is None:
                continue
            extent = ((end + 1) - beg) // step
            if extent.free_symbols:
                continue
            try:
                val = int(extent)
            except (TypeError, ValueError):
                continue
            if val in potential_ranges:
                loops.append(("loop", child, cfg))

    return maps + loops


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Constant propagation + unroll")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    parser.add_argument("--symbols", type=str, required=True,
                        help="Comma-separated key=val pairs, e.g. nclv=5")
    args = parser.parse_args()

    sym_map = {}
    for pair in args.symbols.split(","):
        k, v = pair.split("=")
        sym_map[k.strip()] = int(v.strip())

    sdfg = dace.SDFG.from_file(args.input)
    unroll(sdfg, sym_map)

    out_path = args.output or args.input.replace(".sdfgz", "_unrolled.sdfgz")
    sdfg.save(out_path, compress=True)
    print(f"Saved to {out_path}")
