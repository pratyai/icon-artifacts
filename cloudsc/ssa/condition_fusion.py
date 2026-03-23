"""condition_fusion.py — Fuse ConditionalBlocks inside LoopRegions.

For each LoopRegion whose immediate children form a linear chain containing
at least one ConditionalBlock, fuse everything into a single giant
ConditionalBlock. Each branch of the result contains the full body for one
combination of original branch choices.

Result: LoopRegion → single ConditionalBlock → full body in each branch.
"""

from __future__ import annotations

import copy
import itertools
from typing import Optional

import dace
from dace import symbolic as sym
from dace.properties import CodeBlock
from dace.sdfg.state import (
    LoopRegion, ConditionalBlock, ControlFlowRegion, SDFGState, ControlFlowBlock
)
from dace.sdfg import utils as sdutil
from ssa.graph_utils import (all_identifiers, fresh_name, condition_free_symbols,
                              loop_private_transients, interstate_assigned_symbols)


def _get_linear_chain(loop: LoopRegion):
    """Return (chain, edges) if the loop's immediate children form a linear chain.

    chain: list of immediate child nodes in order
    edges: list of InterstateEdge data between consecutive nodes (len = len(chain)-1)

    Returns None if the children don't form a simple linear chain.
    """
    nodes = list(loop.nodes())
    if not nodes:
        return None

    # Build successor/predecessor maps — must be 1:1
    succ = {}
    pred = {}
    for e in loop.edges():
        if e.src in succ:
            return None  # branching
        succ[e.src] = (e.dst, e.data)
        if e.dst in pred:
            return None  # merging
        pred[e.dst] = (e.src, e.data)

    start = loop.start_block
    if start is None:
        return None

    chain = []
    edges = []
    current = start
    while current is not None:
        chain.append(current)
        if current in succ:
            next_node, edge_data = succ[current]
            edges.append(edge_data)
            current = next_node
        else:
            current = None

    if len(chain) != len(nodes):
        return None  # not all nodes reachable in a single chain

    return chain, edges


def _normalize_branches(cb: ConditionalBlock) -> list[tuple[str, ControlFlowRegion]]:
    """Return branches with explicit conditions (else → negation of all others).

    If the CB has no else branch, synthesize an empty one so the Cartesian
    product covers the "condition is false, do nothing" case.
    """
    branches = []
    cond_parts = []
    has_else = False

    for cond, cfr in cb.branches:
        if cond is not None:
            cond_str = cond.as_string
            cond_parts.append(cond_str)
            branches.append((cond_str, cfr))
        else:
            has_else = True
            branches.append((None, cfr))

    # Build explicit else condition
    if cond_parts:
        else_cond = " and ".join(f"not ({c})" for c in cond_parts)
    else:
        else_cond = "1"

    # Synthesize empty else branch if missing
    if not has_else and cond_parts:
        empty_cfr = ControlFlowRegion(label=f"{cb.label}_empty_else")
        empty_cfr.add_state(f"{cb.label}_noop", is_start_block=True)
        branches.append((None, empty_cfr))

    return [(else_cond if c is None else c, cfr) for c, cfr in branches]


def _inline_cfr(target: ControlFlowRegion, source: ControlFlowRegion,
                prev_node, edge_data, name_ctr: dict) -> ControlFlowBlock:
    """Copy all nodes/edges from source into target, connect prev_node to start.

    name_ctr: mutable dict {"n": int} used as a global counter for unique names.
    Returns the sink node of the inlined content.
    """
    mapping = {}
    for n in source.nodes():
        new_n = copy.deepcopy(n)
        new_n._label = f"{n.label}_cf{name_ctr['n']}"
        name_ctr['n'] += 1
        mapping[id(n)] = new_n
        target.add_node(new_n)

    for e in source.edges():
        target.add_edge(mapping[id(e.src)], mapping[id(e.dst)], copy.deepcopy(e.data))

    # Connect previous node to the start of the inlined block
    start = mapping[id(source.start_block)]
    if prev_node is not None:
        ed = copy.deepcopy(edge_data) if edge_data is not None else dace.InterstateEdge()
        target.add_edge(prev_node, start, ed)

    # Return sink
    sinks = [mapping[id(s)] for s in source.sink_nodes()]
    assert len(sinks) >= 1, f"Inlined CFR has no sink nodes"
    return sinks[0]


def _build_branch_body(chain, edges, choices, label: str,
                       name_ctr: dict) -> ControlFlowRegion:
    """Build a ControlFlowRegion for one combination of branch choices.

    chain: list of nodes (SDFGState, ConditionalBlock, or other)
    edges: list of InterstateEdge data between consecutive chain nodes
    choices: list of (cond_str, cfr) for each ConditionalBlock in chain order
    name_ctr: mutable dict {"n": int} for globally unique block names
    """
    new_cfr = ControlFlowRegion(label=label)

    prev_node = None
    cb_idx = 0

    for i, node in enumerate(chain):
        edge_before = edges[i - 1] if i > 0 else None

        if isinstance(node, ConditionalBlock):
            _, branch_cfr = choices[cb_idx]
            cb_idx += 1
            prev_node = _inline_cfr(new_cfr, branch_cfr, prev_node, edge_before,
                                    name_ctr)

        else:
            # SDFGState or other ControlFlowBlock — deep copy
            new_node = copy.deepcopy(node)
            new_node._label = f"{node.label}_cf{name_ctr['n']}"
            name_ctr['n'] += 1
            new_cfr.add_node(new_node)
            if prev_node is not None:
                ed = copy.deepcopy(edge_before) if edge_before is not None else dace.InterstateEdge()
                new_cfr.add_edge(prev_node, new_node, ed)
            prev_node = new_node

    # Set start block to the node with no predecessors
    if new_cfr.nodes():
        for n in new_cfr.nodes():
            if len(list(new_cfr.predecessors(n))) == 0:
                new_cfr.start_block = new_cfr.node_id(n)
                break

    return new_cfr


def fuse_conditions_in_loop(loop: LoopRegion, sdfg: dace.SDFG) -> bool:
    """Fuse all ConditionalBlocks in a LoopRegion into one giant ConditionalBlock.

    Returns True if fusion was performed.
    """
    result = _get_linear_chain(loop)
    if result is None:
        return False

    chain, edges = result

    # Find ConditionalBlocks in the chain (skip if too many — exponential blowup)
    cb_indices = [i for i, n in enumerate(chain) if isinstance(n, ConditionalBlock)]
    if not cb_indices or len(cb_indices) > 3:
        return False

    # Collect all interstate edge assignments in the chain.
    # For fusion to work, we need to hoist these assignments to before the
    # fused CB.  This is only safe if the RHS of each assignment is
    # loop-invariant (doesn't depend on itervar or anything written in the loop).
    itervar = loop.loop_variable
    _, write_set = loop.read_and_write_sets()
    loop_varying = write_set | {itervar}

    hoisted_assignments = {}  # symbol -> expr_str
    for edge_data in edges:
        for lhs, rhs in edge_data.assignments.items():
            rhs_syms = {str(s) for s in sym.pystr_to_symbolic(rhs).free_symbols}
            if rhs_syms & loop_varying:
                return False  # RHS changes per iteration, can't hoist
            hoisted_assignments[lhs] = rhs

    # Normalize branches for each CB
    cb_branches = []
    for i in cb_indices:
        cb_branches.append(_normalize_branches(chain[i]))

    # Cartesian product of all branch choices
    branch_options = [list(range(len(b))) for b in cb_branches]

    giant_cb = ConditionalBlock(label="fused_conds")
    name_ctr = {"n": 0}

    for combo in itertools.product(*branch_options):
        # Build combined condition
        cond_parts = []
        choices = []
        for cb_num, choice_idx in enumerate(combo):
            cond_str, cfr = cb_branches[cb_num][choice_idx]
            cond_parts.append(f"({cond_str})")
            choices.append((cond_str, cfr))

        combined_cond = " and ".join(cond_parts)

        # Build body
        body = _build_branch_body(chain, edges, choices,
                                  label=f"fused_{'_'.join(str(c) for c in combo)}",
                                  name_ctr=name_ctr)
        # Strip hoisted assignments from intra-branch edges (now on setup edge)
        if hoisted_assignments:
            for e in body.edges():
                for lhs in hoisted_assignments:
                    if lhs in e.data.assignments:
                        del e.data.assignments[lhs]
        giant_cb.add_branch(CodeBlock(combined_cond), body)

    # Make the last branch an implicit else
    if len(giant_cb.branches) > 1:
        giant_cb.branches[-1] = (None, giant_cb.branches[-1][1])

    # Replace loop contents with the single giant CB
    for node in list(loop.nodes()):
        loop.remove_node(node)

    if hoisted_assignments:
        # Add a setup state that fires the hoisted assignments before the CB
        setup = SDFGState(label=f"fused_setup_{name_ctr['n']}")
        name_ctr['n'] += 1
        loop.add_node(setup, is_start_block=True)
        loop.add_node(giant_cb)
        loop.add_edge(setup, giant_cb, dace.InterstateEdge(assignments=hoisted_assignments))
    else:
        loop.add_node(giant_cb, is_start_block=True)

    # Fix parent references
    sdutil.set_nested_sdfg_parent_references(sdfg)

    return True


def _is_hoistable(cb: ConditionalBlock, loop: LoopRegion) -> bool:
    """Check if a CB's conditions are loop-invariant (safe to hoist above the loop).

    Conditions must not depend on the loop variable or anything written in the loop.
    """
    itervar = loop.loop_variable
    _, write_set = loop.read_and_write_sets()

    for cond, _ in cb.branches:
        if cond is None:
            continue
        fsyms = condition_free_symbols(cond)
        if itervar in fsyms:
            return False
        if fsyms & write_set:
            return False
    return True


def _clone_loop_with_cb_replaced(loop: LoopRegion, cb: ConditionalBlock,
                                  branch_cfr: ControlFlowRegion,
                                  label_suffix: str, name_ctr: dict) -> LoopRegion:
    """Deep-copy the loop, replacing the CB node with the inlined branch body."""
    new_loop = LoopRegion(
        label=f"{loop.label}_{label_suffix}",
        condition_expr=loop.loop_condition.as_string,
        loop_var=loop.loop_variable,
        initialize_expr=loop.init_statement.as_string if loop.init_statement else None,
        update_expr=loop.update_statement.as_string if loop.update_statement else None,
        inverted=loop.inverted,
    )

    # Deep-copy all non-CB nodes, map old→new
    node_map = {}
    for n in loop.nodes():
        if n is cb:
            continue
        new_n = copy.deepcopy(n)
        new_n._label = f"{n.label}_h{name_ctr['n']}"
        name_ctr['n'] += 1
        node_map[id(n)] = new_n
        new_loop.add_node(new_n)

    # Inline the branch body in place of CB
    # First, create a mini CFR from the branch and inline its nodes
    branch_nodes = {}
    for bn in branch_cfr.nodes():
        new_bn = copy.deepcopy(bn)
        new_bn._label = f"{bn.label}_h{name_ctr['n']}"
        name_ctr['n'] += 1
        branch_nodes[id(bn)] = new_bn
        new_loop.add_node(new_bn)

    for be in branch_cfr.edges():
        new_loop.add_edge(branch_nodes[id(be.src)], branch_nodes[id(be.dst)],
                          copy.deepcopy(be.data))

    branch_start = branch_nodes[id(branch_cfr.start_block)]
    branch_sinks = [branch_nodes[id(s)] for s in branch_cfr.sink_nodes()]

    # Re-create edges, replacing CB endpoints with branch start/sinks
    for e in loop.edges():
        if e.src is cb and e.dst is cb:
            continue
        elif e.src is cb:
            # CB → next: connect each branch sink → next
            dst = node_map[id(e.dst)]
            for sink in branch_sinks:
                new_loop.add_edge(sink, dst, copy.deepcopy(e.data))
        elif e.dst is cb:
            # prev → CB: connect prev → branch start
            src = node_map[id(e.src)]
            new_loop.add_edge(src, branch_start, copy.deepcopy(e.data))
        else:
            src = node_map[id(e.src)]
            dst = node_map[id(e.dst)]
            new_loop.add_edge(src, dst, copy.deepcopy(e.data))

    # Set start block
    if loop.start_block is cb:
        new_loop._start_block = new_loop.node_id(branch_start)
    else:
        new_start = node_map[id(loop.start_block)]
        new_loop._start_block = new_loop.node_id(new_start)

    return new_loop


def hoist_invariant_conditions(sdfg: dace.SDFG) -> int:
    """Hoist loop-invariant ConditionalBlocks above their enclosing LoopRegion.

    For each LoopRegion containing a CB whose condition doesn't depend on the
    loop variable or anything written in the loop, replace the loop with a CB
    where each branch contains a copy of the loop with the CB replaced by the
    chosen branch body.

    Returns count of hoisted conditions.
    """
    from tqdm import tqdm

    hoisted = 0
    pbar = tqdm(desc="CondHoist", unit="hoist")
    changed = True
    while changed:
        changed = False
        for sd in sdfg.all_sdfgs_recursive():
            for cfg in sd.all_control_flow_regions():
                for loop in list(cfg.nodes()):
                    if not isinstance(loop, LoopRegion):
                        continue
                    # Only hoist when the loop body is just a CB (possibly
                    # preceded by a setup state with interstate edge assignments)
                    nodes = list(loop.nodes())
                    cbs = [n for n in nodes if isinstance(n, ConditionalBlock)]
                    if len(cbs) != 1 or len(nodes) > 2:
                        continue
                    target_cb = cbs[0]
                    if not _is_hoistable(target_cb, loop):
                        continue

                    pbar.set_postfix_str(f"{loop.label}/{target_cb.label}")

                    # Normalize branches (ensure else exists)
                    branches = _normalize_branches(target_cb)

                    # Collect loop-invariant interstate edge assignments that
                    # CB conditions depend on — these must be hoisted to before
                    # the outer CB so the conditions can evaluate correctly.
                    _, write_set = loop.read_and_write_sets()
                    loop_varying = write_set | {loop.loop_variable}

                    cond_syms = set()
                    for cond, _ in target_cb.branches:
                        if cond is not None:
                            cond_syms |= condition_free_symbols(cond)

                    pre_assignments = {}
                    for e in loop.edges():
                        for lhs, rhs in e.data.assignments.items():
                            if lhs in cond_syms:
                                rhs_syms = {str(s) for s in sym.pystr_to_symbolic(rhs).free_symbols}
                                if rhs_syms & loop_varying:
                                    continue
                                pre_assignments[lhs] = rhs

                    # Only loop-private transients are safe to rename per-branch
                    rename_candidates = loop_private_transients(sd, loop)

                    # Build the outer CB that replaces the loop
                    outer_cb = ConditionalBlock(label=f"{loop.label}_hoisted")
                    name_ctr = {"n": 0}
                    taken = all_identifiers(sd)

                    for i, (cond_str, branch_cfr) in enumerate(branches):
                        new_loop = _clone_loop_with_cb_replaced(
                            loop, target_cb, branch_cfr,
                            label_suffix=f"br{i}", name_ctr=name_ctr)

                        # Strip hoisted assignments from cloned loop's internal edges
                        if pre_assignments:
                            for e in new_loop.edges():
                                for lhs in pre_assignments:
                                    if lhs in e.data.assignments:
                                        del e.data.assignments[lhs]

                        # Rename loop-private transients per-branch so sibling
                        # branches don't block each other's privatization/L2M.
                        if len(branches) > 1 and rename_candidates:
                            repl = {}
                            for rname in rename_candidates:
                                new_name = fresh_name(sd, rname, f"_hb{i}", taken)
                                taken.add(new_name)
                                desc = copy.deepcopy(sd.arrays[rname])
                                desc.transient = True
                                sd.add_datadesc(new_name, desc)
                                repl[rname] = new_name
                            for st in new_loop.all_states():
                                st._sdfg = sd
                            new_loop.replace_dict(repl)

                        branch_region = ControlFlowRegion(label=f"{loop.label}_hbr{i}")
                        branch_region.add_node(new_loop, is_start_block=True)

                        if i == len(branches) - 1 and len(branches) > 1:
                            # Last branch becomes else
                            outer_cb.add_branch(None, branch_region)
                        else:
                            outer_cb.add_branch(CodeBlock(cond_str), branch_region)

                    # Replace the loop with the outer CB in the parent CFG
                    in_edges = list(cfg.in_edges(loop))
                    out_edges = list(cfg.out_edges(loop))

                    cfg.add_node(outer_cb)
                    if pre_assignments:
                        # Insert a setup state before the outer CB for hoisted assignments
                        setup = SDFGState(label=f"{loop.label}_hoist_setup")
                        cfg.add_node(setup)
                        for e in in_edges:
                            cfg.add_edge(e.src, setup, e.data)
                        cfg.add_edge(setup, outer_cb,
                                     dace.InterstateEdge(assignments=pre_assignments))
                    else:
                        for e in in_edges:
                            cfg.add_edge(e.src, outer_cb, e.data)
                    for e in out_edges:
                        cfg.add_edge(outer_cb, e.dst, e.data)

                    is_start = cfg.start_block is loop
                    cfg.remove_node(loop)
                    if is_start:
                        if pre_assignments:
                            cfg._start_block = cfg.node_id(setup)
                        else:
                            cfg._start_block = cfg.node_id(outer_cb)

                    hoisted += 1
                    changed = True
                    pbar.update(1)
                    break  # Restart iteration
                if changed:
                    break
            if changed:
                break

    pbar.close()
    if hoisted:
        sdutil.set_nested_sdfg_parent_references(sdfg)
        print(f"Condition hoist: {hoisted} conditions hoisted above loops")
    return hoisted


def fuse_all_conditions(sdfg: dace.SDFG) -> int:
    """Fuse ConditionalBlocks in all LoopRegions. Returns count of fused loops."""
    from tqdm import tqdm

    # Collect candidates
    candidates = []
    for sd in sdfg.all_sdfgs_recursive():
        for cfg in sd.all_control_flow_regions():
            for child in cfg.nodes():
                if isinstance(child, LoopRegion):
                    candidates.append((child, sd))

    fused = 0
    for loop, sd in tqdm(candidates, desc="CondFusion", unit="loop"):
        if fuse_conditions_in_loop(loop, sd):
            fused += 1
    if fused:
        print(f"Condition fusion: {fused} loops fused")
    return fused
