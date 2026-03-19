"""unroll.py — Propagate constant symbols and unroll matching loops.

Two public functions:

- ``propagate_constants(sdfg, symbol_map)`` — replace symbols with concrete
  values while preserving the external interface (non-transient array shapes
  and SDFG symbols).

- ``unroll_loops(sdfg, symbol_map)`` — find loops whose extent matches
  a propagated value and unroll them one at a time.

The legacy ``unroll(sdfg, symbol_map)`` combines both steps.

Usage (standalone):
    python -m ssa.unroll input.sdfgz -o output.sdfgz --symbols nclv=5
"""

from __future__ import annotations

import ast
import copy
from collections import deque
from typing import Dict

import dace
from tqdm import tqdm
from dace import serialize, symbolic
from dace.sdfg import InterstateEdge
from dace.sdfg.nodes import NestedSDFG
from dace.sdfg.state import LoopRegion
from dace.transformation.passes.analysis import loop_analysis
from dace.frontend.python.astutils import ASTFindReplace


def propagate_constants(sdfg: dace.SDFG, symbol_map: Dict[str, int]):
    """Replace symbols with concrete values, preserving non-transient array shapes.

    Non-transient array shapes are snapshotted before replacement and restored
    afterwards so the external data interface stays symbolic.  Symbols that
    remain referenced (e.g. in restored shapes) are re-added to the symbol table.
    """
    orig_shapes: dict[str, list] = {}
    for name, desc in sdfg.arrays.items():
        if not desc.transient:
            orig_shapes[name] = copy.deepcopy(list(desc.shape))

    sdfg.replace_dict(symbol_map)

    for name, shape in orig_shapes.items():
        if name in sdfg.arrays:
            sdfg.arrays[name].shape = shape

    for name, shape in orig_shapes.items():
        if name not in sdfg.arrays:
            continue
        for s in shape:
            for fs in getattr(s, 'free_symbols', set()):
                sym_name = str(fs)
                if sym_name not in sdfg.symbols:
                    sdfg.add_symbol(sym_name, dace.int32)

    print(f"Propagate constants: {symbol_map}")


def unroll_loops(sdfg: dace.SDFG, symbol_map: Dict[str, int]):
    """Unroll loops whose iteration count matches a propagated value.

    Each unroll mutates the graph and invalidates node references, so we
    find-and-unroll one target at a time.
    """
    import time
    potential_ranges = set(symbol_map.values())

    unrolled = 0
    skipped = 0
    round_num = 0
    pbar = tqdm(desc="Unrolling", unit="op")

    while True:
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
        for node, parent in batch:
            pbar.write(f"    loop: {node.label} [{node.loop_variable}]")

        applied_any = False
        for node, parent in batch:
            label = node.label
            pbar.set_postfix_str(label)
            t0 = time.time()
            try:
                _manual_loop_unroll(sdfg, node, parent)
                unrolled += 1
            except (ValueError, TypeError) as e:
                pbar.write(f"    SKIP {label}: {e}")
                skipped += 1
                continue
            applied_any = True
            dt = time.time() - t0
            if dt > 2.0:
                pbar.write(f"    slow: {label} took {dt:.1f}s")
            pbar.update(1)
            pbar.write(f"    === unrolled #{unrolled}: {label} ===")
            break  # Re-collect — graph references are stale

        if not applied_any:
            break

    pbar.close()

    print(f"Unroll: {unrolled} loops"
          f"{f', {skipped} skipped' if skipped else ''}")


def _manual_loop_unroll(sdfg: dace.SDFG, loop: LoopRegion, parent_cfg):
    """Unroll a LoopRegion by copying its body directly into the parent CFG.

    Unlike DaCe's LoopUnroll, this avoids the intermediate ControlFlowRegion +
    .inline() pattern which corrupts certain loop bodies.
    """
    start = loop_analysis.get_init_assignment(loop)
    end = loop_analysis.get_loop_end(loop)
    stride = loop_analysis.get_loop_stride(loop)
    itervar = loop.loop_variable

    if start is None or end is None or stride is None or itervar is None:
        raise ValueError(f"Cannot determine bounds for loop {loop.label}")

    stride_val = int(symbolic.evaluate(stride, sdfg.constants))

    # Compute iteration values: handle both upward and downward loops
    if stride_val > 0:
        iter_values = list(range(int(start), int(end) + 1, stride_val))
    else:
        iter_values = list(range(int(start), int(end) - 1, stride_val))

    if not iter_values:
        raise ValueError(f"Loop {loop.label} has 0 iterations "
                         f"(start={start}, end={end}, stride={stride_val})")

    # ── Phase 1: snapshot everything we need from the loop ────────────
    predecessors = [(e.src, copy.deepcopy(e.data)) for e in parent_cfg.in_edges(loop)]
    successors = [(e.dst, copy.deepcopy(e.data)) for e in parent_cfg.out_edges(loop)]
    was_start = parent_cfg.start_block == loop
    if not was_start:
        saved_start = parent_cfg.start_block

    loop_blocks = list(loop.nodes())
    loop_edges = [(e.src, e.dst, e.data) for e in loop.edges()]
    loop_start = loop.start_block

    # ── Phase 2: create iteration copies (loop still in graph) ─────────
    iteration_first = []
    iteration_last = []

    for current_val in iter_values:
        suffix = f"_{itervar}_{current_val}"
        block_map = {}

        for block in loop_blocks:
            new_block = serialize.from_json(
                serialize.to_json(block),
                context={'sdfg': sdfg},
            )
            new_block.label = new_block.label + suffix
            parent_cfg.add_node(new_block, ensure_unique_name=True)
            block_map[id(block)] = new_block

        copied_set = set(block_map.values())
        for src, dst, data in loop_edges:
            new_src = block_map[id(src)]
            new_dst = block_map[id(dst)]
            new_data = copy.deepcopy(data)
            parent_cfg.add_edge(new_src, new_dst, new_data)

        # Replace loop variable in block contents
        for new_block in block_map.values():
            new_block.replace_dict({itervar: current_val})

        # Replace loop variable in interstate edges within this iteration
        for edge in list(parent_cfg.edges()):
            if edge.src not in copied_set or edge.dst not in copied_set:
                continue
            data = edge.data
            if not isinstance(data, InterstateEdge):
                continue
            if not data.is_unconditional():
                ASTFindReplace({itervar: str(current_val)}).visit(data.condition)
            if data.assignments:
                new_asgn = {}
                for k, v in data.assignments.items():
                    k_ast = ast.parse(k)
                    v_ast = ast.parse(v)
                    ASTFindReplace({itervar: str(current_val)}).visit(k_ast)
                    ASTFindReplace({itervar: str(current_val)}).visit(v_ast)
                    new_asgn[ast.unparse(k_ast)] = ast.unparse(v_ast)
                data.assignments = new_asgn

        # Replace in nested SDFGs' symbol mappings
        for new_block in block_map.values():
            for node, _ in _all_nodes_in_block(new_block):
                if isinstance(node, NestedSDFG) and itervar in node.symbol_mapping:
                    sm = node.symbol_mapping[itervar]
                    if hasattr(sm, 'subs'):
                        node.symbol_mapping[itervar] = sm.subs(
                            {symbolic.symbol(itervar): current_val}
                        )
                    else:
                        node.symbol_mapping[itervar] = current_val

        first = block_map[id(loop_start)]
        iteration_first.append(first)

        last = [b for b in copied_set
                if not any(e.dst in copied_set for e in parent_cfg.out_edges(b))]
        iteration_last.append(last)

    # ── Phase 3: wire and remove ──────────────────────────────────────
    for i in range(len(iteration_first) - 1):
        for terminal in iteration_last[i]:
            parent_cfg.add_edge(terminal, iteration_first[i + 1], InterstateEdge())

    for pred_node, pred_data in predecessors:
        parent_cfg.add_edge(pred_node, iteration_first[0], pred_data)

    for succ_node, succ_data in successors:
        for terminal in iteration_last[-1]:
            parent_cfg.add_edge(terminal, succ_node, succ_data)

    parent_cfg.remove_node(loop)

    if was_start:
        parent_cfg._cached_start_block = iteration_first[0]
    else:
        parent_cfg._cached_start_block = saved_start

    sdfg.reset_cfg_list()

    # ── Sanity check: all nodes reachable from start ──────────────────
    visited = set()
    q = deque([parent_cfg.start_block])
    while q:
        n = q.popleft()
        if n in visited:
            continue
        visited.add(n)
        for e in parent_cfg.out_edges(n):
            q.append(e.dst)
    unreachable = set(parent_cfg.nodes()) - visited
    if unreachable:
        labels = sorted(n.label for n in unreachable)[:10]
        raise RuntimeError(
            f"After unrolling {loop.label}: {len(unreachable)} unreachable "
            f"nodes in {parent_cfg.label}: {labels}"
        )


def _all_nodes_in_block(block):
    """Yield (node, state) for all dataflow nodes in a block (state or nested CFR)."""
    if hasattr(block, 'all_states'):
        for state in block.all_states():
            for node in state.nodes():
                yield node, state
    elif hasattr(block, 'nodes'):
        for node in block.nodes():
            yield node, block


def unroll(sdfg: dace.SDFG, symbol_map: Dict[str, int]):
    """Propagate constants then unroll (legacy combined interface)."""
    propagate_constants(sdfg, symbol_map)
    unroll_loops(sdfg, symbol_map)


# ── Internal helpers ──────────────────────────────────────────────────────


def _collect_unroll_targets(sdfg, potential_ranges):
    """Collect LoopRegions whose extent matches a target range.

    Returns a list of (node, parent_cfg) tuples.
    """
    loops = []
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
                loops.append((child, cfg))
    return loops


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
