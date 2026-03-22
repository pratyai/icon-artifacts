"""unroll.py — Propagate constant symbols and unroll matching loops.

Two public functions:

- ``propagate_constants(sdfg, symbol_map)`` — replace symbols with concrete
  values while preserving the external interface (non-transient array shapes
  and SDFG symbols).

- ``unroll_loops(sdfg, max_iterations)`` — unroll loops with concrete
  iteration count <= max_iterations (default 10).

Usage (standalone):
    python -m ssa.unroll input.sdfgz -o output.sdfgz --symbols nclv=5
"""

from __future__ import annotations

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
from ssa.graph_utils import rename_local_scalars, all_nodes_in_block

_ur_counter = 0

def _next_ur_suffix() -> str:
    """Return a globally unique unroll suffix like __ur1, __ur2, ..."""
    global _ur_counter
    _ur_counter += 1
    return f"__ur{_ur_counter}"


def _compute_iter_values(loop: LoopRegion, sdfg: dace.SDFG):
    """Return (itervar, iter_values) or raise ValueError."""
    start = loop_analysis.get_init_assignment(loop)
    end = loop_analysis.get_loop_end(loop)
    stride = loop_analysis.get_loop_stride(loop)
    itervar = loop.loop_variable

    if start is None or end is None or stride is None or itervar is None:
        raise ValueError(f"Cannot determine bounds for loop {loop.label}")

    stride_val = int(symbolic.evaluate(stride, sdfg.constants))
    if stride_val > 0:
        iter_values = list(range(int(start), int(end) + 1, stride_val))
    else:
        iter_values = list(range(int(start), int(end) - 1, stride_val))

    if not iter_values:
        raise ValueError(
            f"Loop {loop.label} has 0 iterations "
            f"(start={start}, end={end}, stride={stride_val})"
        )
    return itervar, iter_values


def _find_private_scalars(sdfg: dace.SDFG, loop: LoopRegion):
    """Return set of transient scalar names that are write-only and loop-private.

    These are safe to rename per unrolled copy.
    """
    from ssa.graph_utils import loop_interior, writeonly_transient_scalars

    loop_states, _, loop_edge_data = loop_interior(loop)
    candidates = writeonly_transient_scalars(sdfg, loop_states)
    if not candidates:
        return set()

    external_states = set(sdfg.all_states()) - loop_states
    private = set()
    for name in candidates:
        if _is_externally_referenced(sdfg, name, external_states, loop_edge_data):
            continue
        private.add(name)
    return private


def _is_externally_referenced(sdfg, name, external_states, loop_edge_data):
    """Check if `name` is referenced in states or edges outside the loop."""
    for state in external_states:
        for node in state.nodes():
            if isinstance(node, NestedSDFG):
                if name in node.in_connectors or name in node.out_connectors:
                    return True
            elif isinstance(node, dace.nodes.AccessNode) and node.data == name:
                return True
    for edge, _ in sdfg.all_edges_recursive():
        if not isinstance(edge.data, InterstateEdge):
            continue
        if edge.data in loop_edge_data:
            continue
        syms = {str(s) for s in edge.data.free_symbols}
        if name in syms or name in edge.data.assignments:
            return True
    return False


def _replace_in_interstate_edges(parent_cfg, copied_set, repl: dict):
    """Apply whole-word replacements in interstate edges between copied blocks."""
    import re
    for edge in list(parent_cfg.edges()):
        if edge.src not in copied_set or edge.dst not in copied_set:
            continue
        data = edge.data
        if not isinstance(data, InterstateEdge):
            continue
        if not data.is_unconditional():
            cond_str = data.condition.as_string
            for old, new in repl.items():
                cond_str = re.sub(rf"\b{re.escape(old)}\b", new, cond_str)
            data.condition = dace.properties.CodeBlock(cond_str)
        if data.assignments:
            new_asgn = {}
            for k, v in data.assignments.items():
                for old, new in repl.items():
                    k = re.sub(rf"\b{re.escape(old)}\b", new, k)
                    v = re.sub(rf"\b{re.escape(old)}\b", new, v)
                new_asgn[k] = v
            data.assignments = new_asgn


def propagate_constants(sdfg: dace.SDFG, symbol_map: Dict[str, int]):
    """Replace symbols with concrete values, preserving non-transient array shapes.

    Non-transient array shapes are snapshotted before replacement and restored
    afterwards so the external data interface stays symbolic.  Symbols that
    remain referenced (e.g. in restored shapes) are re-added to the symbol table.

    Note: Always run unit tests in tests/test_propagate_constants.py after
    modifying this function.
    """
    orig_shapes: dict[str, list] = {}
    for name, desc in sdfg.arrays.items():
        if not desc.transient:
            orig_shapes[name] = copy.deepcopy(list(desc.shape))

    # Convert values to strings for replacement
    repl = {k: str(v) for k, v in symbol_map.items()}

    # replace_keys=False is CRITICAL: we want to replace OCCURRENCES of symbols
    # in expressions with the constant value, NOT rename the symbol in the table.
    # We must do this recursively for all nested SDFGs.
    for g in sdfg.all_sdfgs_recursive():
        g.replace_dict(repl, replace_keys=False)

    # After replacement, symbols in symbol_map are now constants. Remove them from symbols.
    for name in symbol_map:
        if name in sdfg.symbols:
            sdfg.remove_symbol(name)

    # Restore external shapes
    for name, shape in orig_shapes.items():
        if name in sdfg.arrays:
            sdfg.arrays[name].shape = shape

    # Re-add symbols that are still needed for the external symbolic interface
    for name, shape in orig_shapes.items():
        if name not in sdfg.arrays:
            continue
        for s in shape:
            if symbolic.issymbolic(s):
                for fs in s.free_symbols:
                    sym_name = str(fs)
                    if sym_name not in sdfg.symbols:
                        sdfg.add_symbol(sym_name, dace.int32)

    print(f"Propagate constants: {symbol_map}")


def unroll_loops(sdfg: dace.SDFG, max_iterations: int = 10):
    """Unroll loops with concrete iteration count <= `max_iterations`.

    All symbolic bounds must already be resolved (call propagate_constants first).
    Each unroll mutates the graph and invalidates node references, so we
    find-and-unroll one target at a time.

    Note: Always run unit tests in tests/test_unroll_loops.py after
    modifying this function.
    """
    import time

    unrolled = 0
    skipped = 0
    round_num = 0
    pbar = tqdm(desc="Unrolling", unit="op")

    while True:
        t0 = time.time()
        batch = _collect_unroll_targets(sdfg, max_iterations)
        t_collect = time.time() - t0
        if not batch:
            break
        round_num += 1
        pbar.total = (pbar.n or 0) + len(batch)
        pbar.refresh()

        applied_any = False
        for node, parent in batch:
            label = node.label
            pbar.set_postfix_str(f"round {round_num}: {label}")
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
                pbar.write(f"    slow: {label} took {dt:.1f}s (collect: {t_collect:.1f}s)")
            pbar.update(1)
            break  # Re-collect — graph references are stale

        if not applied_any:
            break

    pbar.close()

    print(f"Unroll: {unrolled} loops{f', {skipped} skipped' if skipped else ''}")


def _manual_loop_unroll(sdfg: dace.SDFG, loop: LoopRegion, parent_cfg):
    """Unroll a LoopRegion by copying its body directly into the parent CFG.

    Unlike DaCe's LoopUnroll, this avoids the intermediate ControlFlowRegion +
    .inline() pattern which corrupts certain loop bodies.
    """
    itervar, iter_values = _compute_iter_values(loop, sdfg)

    # ── Phase 1: snapshot everything we need from the loop ────────────
    predecessors = [(e.src, copy.deepcopy(e.data)) for e in parent_cfg.in_edges(loop)]
    successors = [(e.dst, copy.deepcopy(e.data)) for e in parent_cfg.out_edges(loop)]
    was_start = parent_cfg.start_block == loop
    if not was_start:
        saved_start = parent_cfg.start_block

    loop_blocks = list(loop.nodes())
    loop_edges = [(e.src, e.dst, e.data) for e in loop.edges()]
    loop_start = loop.start_block
    private_scalars = _find_private_scalars(sdfg, loop)

    # ── Phase 2: create iteration copies (loop still in graph) ─────────
    iteration_first = []
    iteration_last = []

    for current_val in iter_values:
        suffix = _next_ur_suffix()
        block_map = {}

        for block in loop_blocks:
            new_block = serialize.from_json(
                serialize.to_json(block),
                context={"sdfg": sdfg},
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

        # Rename loop-private transient scalars so unrolled iterations
        # don't share the same intermediate names.
        if private_scalars:
            scalar_repl = rename_local_scalars(sdfg, block_map.values(), suffix,
                                               only=private_scalars)
            if scalar_repl:
                _replace_in_interstate_edges(parent_cfg, copied_set, scalar_repl)

        # Replace loop variable in interstate edges within this iteration
        _replace_in_interstate_edges(
            parent_cfg, copied_set, {itervar: str(current_val)}
        )

        # Replace in nested SDFGs' symbol mappings
        for new_block in block_map.values():
            for node, _ in all_nodes_in_block(new_block):
                if isinstance(node, NestedSDFG):
                    for sym, mapping in node.symbol_mapping.items():
                        if isinstance(mapping, symbolic.sympy.Basic):
                            node.symbol_mapping[sym] = mapping.subs(
                                {symbolic.symbol(itervar): current_val}
                            )
                        elif isinstance(mapping, str):
                            # Replace in string expressions
                            node.symbol_mapping[sym] = mapping.replace(
                                itervar, str(current_val)
                            )
                        elif mapping == symbolic.symbol(itervar):
                            node.symbol_mapping[sym] = current_val

        first = block_map[id(loop_start)]
        iteration_first.append(first)

        last = [
            b
            for b in copied_set
            if not any(e.dst in copied_set for e in parent_cfg.out_edges(b))
        ]
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


# ── Internal helpers ──────────────────────────────────────────────────────


def _collect_unroll_targets(sdfg, max_iterations: int):
    """Collect LoopRegions with concrete iteration count <= max_iterations.

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

            try:
                b_val = int(symbolic.evaluate(beg, sdfg.constants))
                e_val = int(symbolic.evaluate(end, sdfg.constants))
                s_val = int(symbolic.evaluate(step, sdfg.constants))

                val = (e_val - b_val) // s_val + 1

                if 0 < val <= max_iterations:
                    loops.append((child, cfg))
            except (TypeError, ValueError):
                continue
    return loops


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Constant propagation + unroll")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    parser.add_argument(
        "--symbols",
        type=str,
        required=True,
        help="Comma-separated key=val pairs, e.g. nclv=5",
    )
    args = parser.parse_args()

    sym_map = {}
    for pair in args.symbols.split(","):
        k, v = pair.split("=")
        sym_map[k.strip()] = int(v.strip())

    sdfg = dace.SDFG.from_file(args.input)
    propagate_constants(sdfg, sym_map)
    unroll_loops(sdfg)

    out_path = args.output or args.input.replace(".sdfgz", "_unrolled.sdfgz")
    sdfg.save(out_path, compress=True)
    print(f"Saved to {out_path}")
