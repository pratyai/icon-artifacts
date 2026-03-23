"""scalar_expansion.py — Expand scalars to arrays indexed by loop itervar.

When a scalar blocks LoopToMap because its write index doesn't depend on the
loop's itervar, we promote it to a 1-D array indexed by that itervar.
This lets each iteration write to its own slot, satisfying LoopToMap's
write-index check.

Two-phase design:
  Phase 1 (analyze): Walk the entire SDFG to find every reference to the
      scalar and compute the correct array index for each site.
  Phase 2 (apply):   Create the array, rewrite every site, remove the scalar.

Known-bad expansions (must NOT expand — causes numerical errors):
  - zqxfg_slice__p1              : reads zqx[*, jk-1, *] (outer-carried)
  - zqx_index_51__p1             : reads zqx[*, jk-1, *]
  - zqx_index_52__p1             : reads zqx[*, jk-1, *]
  - zqx_index_53__p1             : reads zqx[*, jk-1, *]
  - zqx_index_54__p1             : reads zqx[*, jk-1, *]
  - zqx_index_55__p1             : reads zqx[*, jk-1, *]
  - tendency_loc_cld_index__p1   : reads tendency_loc_cld[*, jk-1, *]
  - zqx0_index__p1               : reads zqx0[*, jk-1, *]
  - zpfplsx_index__p1            : reads zpfplsx[*, jk-1, *]
  - tendency_loc_t_index_3__p1   : reads tendency_loc_t[jk-1, *]
  - tendency_loc_t_index_4__p1   : reads tendency_loc_t[jk-1, *]
  - zmf_index_0__p1              : TBD (was bad in previous bisection)

Known-good expansions (must expand — verified numerically correct):
  - zcor__v3__p1
  - zlcust_slice__p1
  - zdtdp__p1
"""

import re
import sympy as sp
from enum import Enum, auto
from typing import Set, List, Any

from tqdm import tqdm
import dace
from dace import nodes as nd, symbolic, subsets as sbs
from dace.sdfg.state import LoopRegion, ControlFlowRegion, ConditionalBlock, SDFGState, ControlFlowBlock
from dace.transformation.passes.analysis import loop_analysis
from dace.transformation.interstate.loop_to_map import _check_range

from ssa.graph_utils import (
    topological_sort, rename_access_node_and_tree, update_metadata,
    update_interstate_edge, update_node_content, is_write, is_read,
    collect_memlet_tree_edges, get_enclosing_loop, get_direct_child,
    collect_all_loops, loop_interior, loop_carried_scalars,
    all_identifiers, fresh_name
)


class ExpansionStrategy(Enum):
    """How to handle external accesses to a scalar being expanded."""
    SKIP = auto()          # Too complex, don't expand
    REDIRECT = auto()      # Redirect external accesses to array[0] or array[last]
    INTERNAL_ONLY = auto() # No external accesses exist


# ---------------------------------------------------------------------------
# analysis helpers
# ---------------------------------------------------------------------------

def _get_loop_range(loop: LoopRegion):
    """Return (itervar, start, end, step) or None if not determinable."""
    start = loop_analysis.get_init_assignment(loop)
    end = loop_analysis.get_loop_end(loop)
    step = loop_analysis.get_loop_stride(loop)
    itervar = loop.loop_variable
    if any(x is None for x in (start, end, step, itervar)):
        return None
    return itervar, start, end, step


def _blocked_scalars(sdfg: dace.SDFG, loop: LoopRegion,
                     log=None) -> set[str]:
    """Return set of scalar names that block this loop from LoopToMap."""
    info = _get_loop_range(loop)
    if info is None:
        if log: log.write(f"  {loop.label}: no range info, skip\n")
        return set()
    itervar, start, end, step = info

    _, write_set = loop.read_and_write_sets()
    loop_carried = loop_carried_scalars(sdfg, loop)
    loop_states = set(loop.all_states())

    # Mirror LoopToMap's other_access_nodes: only check scalars that are
    # visible outside the loop (transient accessed elsewhere, or non-transient).
    # Loop-private transients are ignored by LoopToMap so no need to expand.
    other_access_nodes = set()
    for state in sdfg.all_states():
        if state in loop_states:
            continue
        other_access_nodes |= {
            n.data for n in state.data_nodes()
            if n.data in sdfg.arrays and sdfg.arrays[n.data].transient
        }
    for state in loop_states:
        other_access_nodes |= {
            n.data for n in state.data_nodes()
            if n.data in sdfg.arrays and not sdfg.arrays[n.data].transient
        }

    itersym = symbolic.pystr_to_symbolic(itervar)
    a = sp.Wild('a', exclude=[itersym])
    b = sp.Wild('b', exclude=[itersym])

    # Track rejection reasons per scalar
    rejected: dict[str, str] = {}  # name -> reason

    blocked = set()
    seen = set()
    for state in loop_states:
        for dn in state.data_nodes():
            name = dn.data
            if name in seen:
                continue
            if name not in sdfg.arrays:
                continue
            desc = sdfg.arrays[name]
            if not isinstance(desc, dace.data.Scalar) or not desc.transient:
                continue
            seen.add(name)
            if name not in other_access_nodes:
                rejected[name] = "no external access"
                continue
            if name not in write_set:
                rejected[name] = "not in write_set"
                continue
            # Check write edges
            has_blocking_write = False
            for st in loop_states:
                for node in st.data_nodes():
                    if node.data != name:
                        continue
                    for e in st.in_edges(node):
                        if e.data.wcr is not None:
                            continue
                        dst_subset = e.data.get_dst_subset(e, st)
                        if not (dst_subset and _check_range(dst_subset, a, itersym,
                                                             b, step)):
                            has_blocking_write = True
                            break
                    if has_blocking_write:
                        break
                if has_blocking_write:
                    break
            if not has_blocking_write:
                rejected[name] = "writes indexed by itervar"
                continue
            if name in loop_carried:
                rejected[name] = "loop-carried"
                continue
            blocked.add(name)

    if log:
        log.write(f"  {loop.label} (itervar={itervar}): "
                  f"{len(blocked)} blocked, {len(rejected)} rejected\n")
        for name in sorted(blocked):
            log.write(f"    BLOCKED: {name}\n")
        for name, reason in sorted(rejected.items()):
            log.write(f"    skip {name}: {reason}\n")

    return blocked


def _find_enclosing_region(sdfg: dace.SDFG,
                            child: nd.Node | dace.SDFGState | ControlFlowRegion) -> ControlFlowRegion | dace.SDFG | None:
    """Find the immediate parent region of `child`."""
    if isinstance(child, (nd.Node, dace.SDFGState)):
        return child.parent_graph
    for cfg in sdfg.all_control_flow_regions():
        if child in cfg.nodes():
            return cfg
    return None


def _loops_sharing_range(parent: ControlFlowRegion | dace.SDFG, start, end, step) -> list:
    """Find all direct-child LoopRegions of `parent` whose range matches."""
    if parent is None:
        return []
    def _norm(expr):
        expr_str = re.sub(r'_[0-9]+(?![a-zA-Z0-9_])', '', str(expr))
        try:
            return symbolic.pystr_to_symbolic(expr_str)
        except Exception:
            return symbolic.pystr_to_symbolic(str(expr))

    matching = []
    n_start, n_end, n_step = _norm(start), _norm(end), _norm(step)

    for child in parent.nodes():
        if not isinstance(child, LoopRegion):
            continue
        info = _get_loop_range(child)
        if info is None:
            continue
        _, c_start, c_end, c_step = info
        if (_norm(c_start) == n_start and
                _norm(c_end) == n_end and
                _norm(c_step) == n_step):
            matching.append(child)
    return matching


def _analyze_external_accesses(sdfg: dace.SDFG, name: str, loops: Set[LoopRegion],
                              parent: ControlFlowRegion) -> ExpansionStrategy:
    """Analyze accesses outside the loops to determine expansion strategy."""
    topo = topological_sort(parent)

    first_loop_idx = None
    last_loop_idx = None
    for i, node in enumerate(topo):
        if node in loops:
            if first_loop_idx is None: first_loop_idx = i
            last_loop_idx = i

    access_before = False
    access_after = False
    access_inter = False

    covered_states = set()
    for lp in loops:
        lp_states, _, _ = loop_interior(lp)
        covered_states |= lp_states

    for i, child in enumerate(topo):
        child_states = list(child.all_states()) if isinstance(child, (ControlFlowRegion, ConditionalBlock)) else [child] if isinstance(child, SDFGState) else []
        has_access = False
        for state in child_states:
            if state in covered_states: continue
            if any(isinstance(n, nd.AccessNode) and n.data == name for n, _ in state.all_nodes_recursive()):
                has_access = True; break

        if has_access:
            if first_loop_idx is None: access_before = True
            elif i < first_loop_idx: access_before = True
            elif i > last_loop_idx: access_after = True
            else: access_inter = True

    # Inter-loop accesses are still inside the parent loop's iteration scope,
    # so they can be safely redirected to array[itervar - offset].
    has_external = access_before or access_after or access_inter
    return ExpansionStrategy.REDIRECT if has_external else ExpansionStrategy.INTERNAL_ONLY


# ---------------------------------------------------------------------------
# Phase 1: Build expansion plan
# ---------------------------------------------------------------------------

def _find_enclosing_target_loop(obj, target_loops: Set[LoopRegion]):
    """Walk up the loop hierarchy to find the nearest target loop enclosing obj."""
    loop = get_enclosing_loop(obj)
    while loop is not None:
        if loop in target_loops:
            return loop
        loop = get_enclosing_loop(loop)
    return None


def _build_expansion_plan(sdfg: dace.SDFG, scalar_name: str, info: dict,
                          exclude_loops: Set[LoopRegion] | None = None) -> List[tuple]:
    """Collect every reference to `scalar_name` in the SDFG with its index.

    Returns a list of (site_type, obj1, obj2, idx_expr) tuples:
      ("access_node", state, node, idx)
      ("node_content", state, node, idx)
      ("metadata", block, None, idx)
      ("edge", edge_data, None, idx)

    Sites inside `exclude_loops` are skipped (they belong to a different target).
    """
    loops = info["loops"]
    parent = info["parent"]
    itervar_offset = symbolic.pystr_to_symbolic(info["offset"])
    last_idx = symbolic.pystr_to_symbolic(info["end"]) - itervar_offset

    # States/blocks belonging to other targets — skip these
    excluded_states: Set[dace.SDFGState] = set()
    excluded_blocks: Set[ControlFlowBlock] = set()
    if exclude_loops:
        for lp in exclude_loops:
            excluded_states |= set(lp.all_states())
            excluded_blocks |= set(lp.all_control_flow_blocks())
            excluded_blocks.add(lp)

    # Topo order within the parent region — used for before/after distinction
    topo = topological_sort(parent)
    topo_idx = {node: i for i, node in enumerate(topo)}
    loops_in_topo = [lp for lp in loops if lp in topo_idx]
    first_loop_idx = min(topo_idx[lp] for lp in loops_in_topo) if loops_in_topo else 0
    last_loop_idx = max(topo_idx[lp] for lp in loops_in_topo) if loops_in_topo else 0

    # The itervar-based index (e.g. jl - kidia) for accesses at the same scope as target loops
    itervar_idx = None
    for lp in loops:
        l_info = _get_loop_range(lp)
        if l_info:
            itervar_idx = symbolic.pystr_to_symbolic(l_info[0]) - symbolic.pystr_to_symbolic(l_info[1])
            break

    def resolve_idx(obj):
        """Determine the array index for a site based on its loop context."""
        target_loop = _find_enclosing_target_loop(obj, loops)
        if target_loop is not None:
            l_info = _get_loop_range(target_loop)
            if l_info:
                return symbolic.pystr_to_symbolic(l_info[0]) - symbolic.pystr_to_symbolic(l_info[1])
        # Not inside any target loop — check topo position in parent
        child = get_direct_child(parent, obj)
        if child is not None and child in topo_idx:
            if topo_idx[child] < first_loop_idx:
                return sp.Integer(0)
            if topo_idx[child] <= last_loop_idx and itervar_idx is not None:
                # Inter-loop: same scope as target loops, use itervar index
                return itervar_idx
        return last_idx

    pattern = re.compile(r'\b' + re.escape(scalar_name) + r'\b')
    plan = []

    # 1. States: AccessNodes and node content
    for state in sdfg.all_states():
        if state in excluded_states:
            continue
        has_any = False
        for node in state.nodes():
            if isinstance(node, nd.AccessNode) and node.data == scalar_name:
                has_any = True
                break
        if not has_any:
            # Also check non-AccessNode references
            for node in state.nodes():
                if not isinstance(node, nd.AccessNode):
                    for e in list(state.in_edges(node)) + list(state.out_edges(node)):
                        if e.data.data == scalar_name:
                            has_any = True; break
                    if not has_any and scalar_name in {str(s) for s in node.free_symbols}:
                        has_any = True
                    if not has_any and isinstance(node, nd.Tasklet) and \
                       pattern.search(node.code.as_string):
                        has_any = True
                if has_any:
                    break
        if not has_any:
            continue

        idx = resolve_idx(state)
        for node in state.nodes():
            if isinstance(node, nd.AccessNode) and node.data == scalar_name:
                plan.append(("access_node", state, node, idx))
            elif not isinstance(node, nd.AccessNode):
                has_ref = False
                for e in list(state.in_edges(node)) + list(state.out_edges(node)):
                    if e.data.data == scalar_name:
                        has_ref = True; break
                if not has_ref and scalar_name in {str(s) for s in node.free_symbols}:
                    has_ref = True
                if not has_ref and isinstance(node, nd.Tasklet) and \
                   pattern.search(node.code.as_string):
                    has_ref = True
                if has_ref:
                    plan.append(("node_content", state, node, idx))

    # 2. Metadata blocks (LoopRegion conditions, ConditionalBlock conditions)
    for block in sdfg.all_control_flow_blocks():
        if block in excluded_blocks:
            continue
        has_ref = False
        if isinstance(block, LoopRegion):
            for attr in (block.loop_condition, block.init_statement, block.update_statement):
                if attr and pattern.search(attr.as_string):
                    has_ref = True; break
        elif isinstance(block, ConditionalBlock):
            for cond, _ in block.branches:
                if cond and pattern.search(cond.as_string):
                    has_ref = True; break
        if has_ref:
            idx = resolve_idx(block)
            plan.append(("metadata", block, None, idx))

    # 3. Interstate edges
    for edge, _ in sdfg.all_edges_recursive():
        if not isinstance(edge.data, dace.InterstateEdge):
            continue
        if edge.src in excluded_states or edge.src in excluded_blocks:
            continue
        has_ref = False
        if edge.data.condition and pattern.search(edge.data.condition.as_string):
            has_ref = True
        if not has_ref:
            for k, v in edge.data.assignments.items():
                if k == scalar_name or pattern.search(str(v)):
                    has_ref = True; break
        if has_ref:
            idx = resolve_idx(edge.src)
            plan.append(("edge", edge.data, None, idx))

    return plan


# ---------------------------------------------------------------------------
# Phase 2: Apply expansion plan
# ---------------------------------------------------------------------------

def _apply_expansion_plan(sdfg: dace.SDFG, scalar_name: str, ext_name: str,
                          plan: List[tuple]):
    """Apply all mutations from the plan."""
    for site_type, obj1, obj2, idx in plan:
        repl_str = f"{ext_name}[{idx}]"

        if site_type == "access_node":
            state, node = obj1, obj2
            edges_to_rename = collect_memlet_tree_edges(state, node)
            rename_access_node_and_tree(state, node, scalar_name, ext_name)
            for e in edges_to_rename:
                if e.data.data == ext_name:
                    e.data.try_initialize(sdfg, state, e)
                    e.data.subset = sbs.Range([(idx, idx, 1)])

        elif site_type == "node_content":
            node = obj2
            update_node_content(node, {scalar_name: repl_str})

        elif site_type == "metadata":
            block = obj1
            update_metadata(block, {scalar_name: repl_str})

        elif site_type == "edge":
            edge_data = obj1
            update_interstate_edge(edge_data, {scalar_name: repl_str})


# ---------------------------------------------------------------------------
# target discovery + public API
# ---------------------------------------------------------------------------

def find_expansion_targets(sdfg: dace.SDFG, force: set[str] | None = None,
                           log=None) -> dict[str, list[dict]]:
    """Identify scalars and loops that are candidates for expansion."""
    all_loops = collect_all_loops(sdfg)
    expansion_targets: dict[str, list[dict]] = {}

    for loop in all_loops:
        if force:
            blocked = set()
            # Check for usage in conditions, metadata, and body
            for cfg in loop.all_control_flow_regions():
                if isinstance(cfg, ConditionalBlock):
                    for cond, _ in cfg.branches:
                        if cond and any(re.search(r"\b" + re.escape(f) + r"\b", cond.as_string) for f in force):
                            for f in force:
                                if re.search(r"\b" + re.escape(f) + r"\b", cond.as_string): blocked.add(f)
                elif isinstance(cfg, LoopRegion):
                    for val in (cfg.init_statement, cfg.loop_condition, cfg.update_statement):
                        if val and any(re.search(r"\b" + re.escape(f) + r"\b", val.as_string) for f in force):
                            for f in force:
                                if re.search(r"\b" + re.escape(f) + r"\b", val.as_string): blocked.add(f)
                for edge in cfg.edges():
                    if isinstance(edge.data, dace.InterstateEdge):
                        for f in force:
                            if (edge.data.condition and re.search(r"\b" + re.escape(f) + r"\b", edge.data.condition.as_string)) or \
                               any(re.search(r"\b" + re.escape(f) + r"\b", str(v)) for v in edge.data.assignments.values()):
                                blocked.add(f)
            for s in loop.all_states():
                for node in s.nodes():
                    if isinstance(node, nd.Tasklet) and any(re.search(r"\b" + re.escape(f) + r"\b", node.code.as_string) for f in force):
                        for f in force:
                            if re.search(r"\b" + re.escape(f) + r"\b", node.code.as_string): blocked.add(f)
                    if isinstance(node, (nd.Tasklet, nd.NestedSDFG)):
                        for fs in node.free_symbols:
                            if fs in force: blocked.add(fs)
                    if isinstance(node, nd.NestedSDFG):
                        for f in force:
                            if f in node.sdfg.free_symbols or f in node.in_connectors or f in node.out_connectors:
                                blocked.add(f)
            read_set, write_set = loop.read_and_write_sets()
            for rw in (read_set | write_set):
                if rw in force: blocked.add(rw)
        else:
            blocked = _blocked_scalars(sdfg, loop, log=log)

        if not blocked: continue
        info = _get_loop_range(loop)
        if info is None: continue
        itervar, start, end, step = info
        parent = _find_enclosing_region(sdfg, loop) or sdfg
        siblings = _loops_sharing_range(parent, start, end, step)
        if loop not in siblings: siblings.append(loop)

        if not blocked: continue

        itervar_base = itervar.split("__")[0]
        if itervar_base == "jl": dim_size = symbolic.pystr_to_symbolic("klon")
        elif itervar_base == "jk": dim_size = symbolic.pystr_to_symbolic("klev")
        elif itervar_base in ("jm", "jn", "jo"): dim_size = symbolic.pystr_to_symbolic("nclv")
        else:
            dim_size = sp.simplify((symbolic.pystr_to_symbolic(end) - symbolic.pystr_to_symbolic(start) + symbolic.pystr_to_symbolic(step)) / symbolic.pystr_to_symbolic(step))

        for name in blocked:
            if name in str(dim_size): continue
            if name not in expansion_targets: expansion_targets[name] = []
            found = False
            for target in expansion_targets[name]:
                if (target["dim_size"] == dim_size and target["offset"] == start and target["parent"] == parent):
                    target["loops"].update(siblings); found = True; break
            if not found:
                expansion_targets[name].append({
                    "start": start, "end": end, "dim_size": dim_size,
                    "offset": start, "loops": set(siblings), "parent": parent,
                })
    # Filter out scalars whose write sources reference an enclosing loop's
    # itervar with an offset (e.g. zqx[jk-1, jl]). These read from a previous
    # outer-loop iteration; expanding along the inner loop doesn't help and
    # can break correctness when the inner loop becomes a parallel Map.
    _outer_carried = set()
    for name in list(expansion_targets):
        for state in sdfg.all_states():
            for node in state.nodes():
                if not (isinstance(node, nd.AccessNode) and node.data == name):
                    continue
                for e in state.in_edges(node):
                    if not isinstance(e.src, nd.AccessNode):
                        continue
                    subset_str = str(e.data.subset) if e.data.subset else ''
                    # Check for "itervar - N" pattern in any dimension
                    for part in subset_str.split(','):
                        part = part.strip()
                        if '- 1' in part or '+ 1' in part:
                            # Check if it references an enclosing loop itervar
                            for sym_str in [str(s) for s in e.data.subset.free_symbols]:
                                if sym_str.startswith('jk'):
                                    _outer_carried.add(name)
    for name in _outer_carried:
        del expansion_targets[name]
    if _outer_carried:
        print(f"  Filtered {len(_outer_carried)} outer-carried scalars: "
              f"{sorted(_outer_carried)}")

    # Assert all multi-target scalars share the same dim/offset
    for name, tlist in expansion_targets.items():
        if len(tlist) > 1:
            dim0, off0 = str(tlist[0]["dim_size"]), str(tlist[0]["offset"])
            for t in tlist[1:]:
                assert str(t["dim_size"]) == dim0 and str(t["offset"]) == off0, \
                    f"BUG: {name} has targets with different dim/offset"

    # Log known-good base names — after privatization they may no longer
    # need expansion (each loop got its own copy), so this is informational.
    _KNOWN_GOOD_BASES = {"zcor__v3", "zlcust_slice", "zdtdp"}
    target_bases = {name.rsplit('__p', 1)[0] if '__p' in name else name
                    for name in expansion_targets}
    for base in _KNOWN_GOOD_BASES:
        if base not in target_bases:
            print(f"  Note: '{base}' not in expansion targets (likely privatized)")

    return expansion_targets


def apply_scalar_expansion(sdfg: dace.SDFG, name: str, targets: list[dict], force: set[str] | None = None):
    """Apply expansion for a single scalar — in-place promotion.

    All targets must share the same dim_size and offset (verified by assertion).
    The scalar descriptor is promoted to a 1-D array with the same name.
    """
    if name not in sdfg.arrays or not isinstance(sdfg.arrays[name], dace.data.Scalar): return 0

    # Merge all target loops into a single unified info (same dim/offset guaranteed)
    dim_size = targets[0]["dim_size"]
    offset = targets[0]["offset"]
    all_loops: Set[LoopRegion] = set()
    # Use the first target's parent as representative (all share same dim)
    for t in targets:
        assert str(t["dim_size"]) == str(dim_size) and str(t["offset"]) == str(offset), \
            f"BUG: {name} has targets with different dim/offset"
        all_loops |= t["loops"]

    # Use the first target as representative info for plan building
    unified_info = {
        "start": targets[0]["start"],
        "end": targets[0]["end"],
        "dim_size": dim_size,
        "offset": offset,
        "loops": all_loops,
        "parent": targets[0]["parent"],
    }

    # Build a single plan covering all target loops (no exclusions)
    plan = _build_expansion_plan(sdfg, name, unified_info, exclude_loops=None)

    if not plan:
        print(f"Skipping expansion for {name}: empty plan")
        return 0

    # Create a single _ext array shared across all targets
    old_desc = sdfg.arrays[name]
    shape = [symbolic.pystr_to_symbolic(str(dim_size)).subs(sdfg.constants)]
    ext_name = fresh_name(sdfg, name, "_ext")
    sdfg.add_transient(ext_name, shape=shape, dtype=old_desc.dtype,
                       storage=old_desc.storage)

    # Apply mutations (rewrite all sites to use ext_name[idx])
    _apply_expansion_plan(sdfg, name, ext_name, plan)

    # Remove the original scalar
    if name in sdfg.arrays:
        sdfg.remove_data(name, validate=False)

    return 1


def _assert_no_data_in_conditions(sdfg: dace.SDFG):
    """Assert that ConditionalBlock conditions don't reference data descriptors directly.

    Call lift_data_refs_in_conditions() before scalar expansion to fix this.
    """
    for cfg in sdfg.all_control_flow_regions():
        for block in cfg.nodes():
            if isinstance(block, ConditionalBlock):
                for cond, _ in block.branches:
                    if cond is None:
                        continue
                    cond_str = cond.as_string
                    for name, desc in sdfg.arrays.items():
                        if isinstance(desc, dace.data.Scalar) and \
                           re.search(r'\b' + re.escape(name) + r'\b', cond_str):
                            raise AssertionError(
                                f"ConditionalBlock '{block.label}' condition references "
                                f"data '{name}' directly. Run lift_data_refs_in_conditions() first."
                            )


def expand_scalars(sdfg: dace.SDFG, force: set[str] | None = None,
                    diagnose: bool = False) -> int:
    """Expand scalars that block LoopToMap in loops.

    If diagnose=True, save a checkpoint after each scalar expansion
    so you can bisect which one breaks numerical correctness.
    """
    _assert_no_data_in_conditions(sdfg)

    log_path = "expand_log.txt"
    log = open(log_path, "w") if diagnose else None
    expansion_targets = find_expansion_targets(sdfg, force, log=log)

    if log:
        log.write(f"\nExpansion targets ({len(expansion_targets)}):\n")
        for name, targets in expansion_targets.items():
            for t in targets:
                loops_str = ", ".join(l.label for l in t["loops"])
                log.write(f"  {name}: dim={t['dim_size']} offset={t['offset']} "
                          f"loops=[{loops_str}]\n")
        log.write("\n")
        log.flush()

    total_expanded = 0
    pbar = tqdm(expansion_targets.items(), desc="Scalar expansion", unit="scalar")
    for name, targets in pbar:
        pbar.set_postfix_str(name)
        if log:
            for i, info in enumerate(targets):
                loops = info["loops"]
                strategy = _analyze_external_accesses(sdfg, name, loops, info["parent"])
                log.write(f"  {name} target {i}: strategy={strategy.name}\n")
            log.flush()
        n = apply_scalar_expansion(sdfg, name, targets, force)
        total_expanded += n
        if log:
            status = f"expanded ({n} targets)" if n > 0 else "skipped"
            log.write(f"{name}: {status}\n")
            log.flush()
            if n > 0:
                path = f"after_expand_{name}.sdfgz"
                sdfg.save(path, compress=True)
                log.write(f"  checkpoint: {path}\n")
                log.flush()
    pbar.close()

    if log:
        log.close()
        print(f"  Expansion log: {log_path}")

    print(f"Scalar expansion: {total_expanded} scalars expanded")
    return total_expanded


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scalar expansion")
    parser.add_argument("input", help="Input SDFG file")
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()
    sdfg = dace.SDFG.from_file(args.input)
    expand_scalars(sdfg)
    out_path = args.output or args.input.replace(".sdfgz", "_expanded.sdfgz")
    sdfg.save(out_path)
    print(f"Saved to {out_path}")
