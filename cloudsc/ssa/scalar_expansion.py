"""scalar_expansion.py — Expand scalars to arrays indexed by inner-loop itervar.

When a scalar blocks LoopToMap because its write index doesn't depend on the
inner loop's itervar, we promote it to a 1-D array indexed by that itervar.
This lets each iteration write to its own slot, satisfying LoopToMap's
write-index check.

Safety constraint: a scalar is only expanded if ALL its accesses within the
parent loop are inside inner loops with compatible ranges.  Accesses "loose"
in the parent body (outside any inner loop) would not have an itervar in
scope to index with, so those scalars are skipped.

Usage (standalone):
    python -m ssa.scalar_expansion input.sdfgz -o output.sdfgz
"""

from __future__ import annotations

import copy
import sympy as sp
from tqdm import tqdm

import dace
from dace import nodes as nd, symbolic, subsets as sbs
from dace.sdfg.state import LoopRegion
from dace.transformation.passes.analysis import loop_analysis
from dace.transformation.interstate.loop_to_map import _check_range


def _get_loop_range(loop: LoopRegion):
    """Return (itervar, start, end, step) or None if not determinable."""
    start = loop_analysis.get_init_assignment(loop)
    end = loop_analysis.get_loop_end(loop)
    step = loop_analysis.get_loop_stride(loop)
    itervar = loop.loop_variable
    if any(x is None for x in (start, end, step, itervar)):
        return None
    return itervar, start, end, step


def _blocked_scalars(sdfg: dace.SDFG, loop: LoopRegion) -> set[str]:
    """Return set of scalar names that block this loop from LoopToMap.

    A scalar blocks if it's in `other_access_nodes`, is written in the loop,
    and the write index doesn't depend on the itervar.
    """
    info = _get_loop_range(loop)
    if info is None:
        return set()
    itervar, start, end, step = info

    _, write_set = loop.read_and_write_sets()
    loop_states = set(loop.all_states())

    # Replicate LoopToMap's other_access_nodes computation
    other_access_nodes = set()
    for state in sdfg.states():
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

    blocked = set()
    for state in loop_states:
        for dn in state.data_nodes():
            if dn.data not in other_access_nodes:
                continue
            if dn.data not in write_set:
                continue
            if dn.data not in sdfg.arrays:
                continue
            desc = sdfg.arrays[dn.data]
            if not isinstance(desc, dace.data.Scalar) or not desc.transient:
                continue
            for e in state.in_edges(dn):
                if e.data.wcr is not None:
                    continue
                dst_subset = e.data.get_dst_subset(e, state)
                if not (dst_subset and _check_range(dst_subset, a, itersym,
                                                     b, step)):
                    blocked.add(dn.data)
    return blocked


def _find_enclosing_loop(sdfg: dace.SDFG,
                          inner: LoopRegion) -> LoopRegion | None:
    """Find the immediate parent LoopRegion of `inner`."""
    for cfg in sdfg.all_control_flow_regions():
        if not isinstance(cfg, LoopRegion):
            continue
        for child in cfg.nodes():
            if child is inner:
                return cfg
    return None


def _loops_sharing_range(parent: LoopRegion, start, end, step) -> list:
    """Find all direct-child LoopRegions of `parent` whose range matches.
    Normalizes SSA version suffixes to ensure matching bounds.
    """
    import re
    def _norm(expr):
        # Strip SSA version suffixes (e.g., _43) from variable names for comparison
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


def _loop_writes_before_reads(loop: LoopRegion, name: str) -> bool:
    """Check if the first access to `name` in the loop body is a write.

    Uses topological order of the loop's CFG children.  Returns True only
    if the first encountered access is a pure write (no read component).
    """
    import networkx as nx
    G = nx.DiGraph()
    for n in loop.nodes():
        G.add_node(n)
    for e in loop.edges():
        G.add_edge(e.src, e.dst)
    topo = list(nx.topological_sort(G))

    for child in topo:
        if isinstance(child, LoopRegion):
            child_states = list(child.all_states())
        elif isinstance(child, dace.SDFGState):
            child_states = [child]
        else:
            child_states = []
            if hasattr(child, 'all_states'):
                child_states = list(child.all_states())

        for state in child_states:
            for node in state.nodes():
                if isinstance(node, nd.AccessNode) and node.data == name:
                    is_r = state.out_degree(node) > 0
                    is_w = state.in_degree(node) > 0
                    if is_w and not is_r:
                        return True   # first access is pure write
                    else:
                        return False  # first access reads
    return False  # no access found


def _loose_accesses_before_loops(parent: LoopRegion, name: str,
                                  compatible_loops: list[LoopRegion]) -> bool:
    """Check that all loose accesses to `name` come topologically before
    any compatible inner loop in the parent's CFG.
    """
    import networkx as nx
    G = nx.DiGraph()
    for n in parent.nodes():
        G.add_node(n)
    for e in parent.edges():
        G.add_edge(e.src, e.dst)
    topo = list(nx.topological_sort(G))

    compatible_set = set(compatible_loops)
    covered_states = set()
    for loop in compatible_loops:
        covered_states |= set(loop.all_states())

    first_loop_idx = None
    last_loose_idx = None

    for i, child in enumerate(topo):
        if child in compatible_set:
            if first_loop_idx is None:
                first_loop_idx = i

        # Check if this child has a loose access
        if isinstance(child, dace.SDFGState):
            child_states = [child]
        elif hasattr(child, 'all_states'):
            child_states = list(child.all_states())
        else:
            child_states = []

        for state in child_states:
            if state in covered_states:
                continue
            for node, _ in state.all_nodes_recursive():
                if isinstance(node, nd.AccessNode) and node.data == name:
                    last_loose_idx = i
                    break

    if last_loose_idx is None:
        return True   # no loose accesses
    if first_loop_idx is None:
        return False  # loose accesses but no loops
    return last_loose_idx < first_loop_idx


def _loose_accesses_after_loops(parent: LoopRegion, name: str,
                                 compatible_loops: list[LoopRegion]) -> bool:
    """Check that all loose accesses to `name` come topologically after
    all compatible inner loops in the parent's CFG.
    """
    import networkx as nx
    G = nx.DiGraph()
    for n in parent.nodes():
        G.add_node(n)
    for e in parent.edges():
        G.add_edge(e.src, e.dst)
    topo = list(nx.topological_sort(G))

    compatible_set = set(compatible_loops)
    covered_states = set()
    for loop in compatible_loops:
        covered_states |= set(loop.all_states())

    last_loop_idx = None
    first_loose_idx = None

    for i, child in enumerate(topo):
        if child in compatible_set:
            last_loop_idx = i

        if isinstance(child, dace.SDFGState):
            child_states = [child]
        elif hasattr(child, 'all_states'):
            child_states = list(child.all_states())
        else:
            child_states = []

        for state in child_states:
            if state in covered_states:
                continue
            for node, _ in state.all_nodes_recursive():
                if isinstance(node, nd.AccessNode) and node.data == name:
                    if first_loose_idx is None:
                        first_loose_idx = i
                    break

    if first_loose_idx is None:
        return True   # no loose accesses
    if last_loop_idx is None:
        return False
    return first_loose_idx > last_loop_idx


def expand_scalars(sdfg: dace.SDFG, force: set[str] | None = None) -> int:
    """Expand scalars that block LoopToMap in inner loops.

    Returns number of scalars expanded.
    """
    # Collect all LoopRegions
    all_loops: list[LoopRegion] = []
    for cfg in sdfg.all_control_flow_regions():
        for child in cfg.nodes():
            if isinstance(child, LoopRegion):
                all_loops.append(child)

    # name -> list of expansion targets
    expansion_targets: dict[str, list[dict]] = {}

    for loop in tqdm(all_loops, desc="Finding blocked scalars", unit="loop"):
        if force:
            blocked = set()
            read_set, write_set = loop.read_and_write_sets()
            rw_set = read_set | write_set
            for f in force:
                for rw in rw_set:
                    if f in rw:
                        blocked.add(rw)
        else:
            blocked = _blocked_scalars(sdfg, loop)

        if not blocked:
            continue

        info = _get_loop_range(loop)
        if info is None:
            continue
        itervar, start, end, step = info

        parent = _find_enclosing_loop(sdfg, loop)
        if parent is None:
            continue

        # Find sibling loops with same range
        siblings = _loops_sharing_range(parent, start, end, step)

        # Compute dimension size, ensuring only top-level symbols are used
        dim_size = (symbolic.pystr_to_symbolic(end)
                    - symbolic.pystr_to_symbolic(start)
                    + symbolic.pystr_to_symbolic(step))
        dim_size = sp.simplify(dim_size / symbolic.pystr_to_symbolic(step))

        # Top-level symbols fallback
        top_symbols = set(sdfg.symbols.keys()) | set(sdfg.constants.keys())
        dim_free = {str(s) for s in dim_size.free_symbols}
        if dim_free - top_symbols:
            itervar_base = itervar.split('__')[0]
            dim_map = {'jl': 'klon', 'jk': 'klev', 'jm': 'nclv',
                       'jn': 'nclv', 'jo': 'nclv'}
            fallback = dim_map.get(itervar_base)
            if fallback and fallback in top_symbols:
                dim_size = symbolic.pystr_to_symbolic(fallback)
            else:
                continue

        for scalar_name in blocked:
            if scalar_name not in expansion_targets:
                expansion_targets[scalar_name] = []
            
            found = False
            for target in expansion_targets[scalar_name]:
                if (target['dim_size'] == dim_size and 
                    target['offset'] == start):
                    target['loops'].update(siblings)
                    found = True
                    break
            
            if not found:
                expansion_targets[scalar_name].append({
                    'start': start,
                    'end': end,
                    'dim_size': dim_size,
                    'offset': start,
                    'loops': set(siblings),
                    'parent': parent,
                })

    expanded = 0
    skipped = 0
    for name, targets in expansion_targets.items():
        if name not in sdfg.arrays:
            continue
        desc = sdfg.arrays[name]
        if not isinstance(desc, dace.data.Scalar):
            continue

        for info in targets:
            # Re-read name in case it was renamed by a previous target of the same scalar
            current_name = name
            if current_name not in sdfg.arrays:
                # Find the most recent expansion name
                for aname in sdfg.arrays:
                    if aname.startswith(name + "_ext"):
                        current_name = aname
            
            if current_name not in sdfg.arrays:
                continue

            access_loops = list(info['loops'])
            parent = info['parent']

            # Safety check
            all_in_loops = True
            covered_states = set()
            for lp in access_loops:
                covered_states |= set(lp.all_states())
            
            for state in parent.all_states():
                if state in covered_states:
                    continue
                for node, _ in state.all_nodes_recursive():
                    if isinstance(node, nd.AccessNode) and node.data == current_name:
                        all_in_loops = False
                        break
                if not all_in_loops:
                    break

            loose_mode = None
            if not all_in_loops:
                if force and any(f in name for f in force):
                    loose_mode = None # Just expand what we can
                elif (_loose_accesses_before_loops(parent, current_name, access_loops)
                        and all(_loop_writes_before_reads(lp, current_name)
                                for lp in access_loops)):
                    loose_mode = 'A'
                elif (_loose_accesses_after_loops(parent, current_name, access_loops)
                        and all(_loop_writes_before_reads(lp, current_name)
                                for lp in access_loops)):
                    loose_mode = 'B'
                else:
                    skipped += 1
                    continue

            ext_name = sdfg.add_transient(
                name + "_ext", 
                shape=[info['dim_size']], 
                dtype=desc.dtype,
                storage=desc.storage,
                find_new_name=True
            )[0]

            itervar_offset = symbolic.pystr_to_symbolic(info['offset'])
            
            # Reindex ALL states in parent recursively
            for state in parent.all_states():
                # Find if this state belongs to a loop inside parent
                local_loop = None
                for cfg in sdfg.all_control_flow_regions():
                    if isinstance(cfg, LoopRegion) and state in cfg.all_states():
                        # innermost loop check
                        if local_loop is None or len(list(cfg.all_states())) < len(list(local_loop.all_states())):
                            local_loop = cfg
                
                # Default index
                idx_expr = None
                if local_loop:
                    l_info = _get_loop_range(local_loop)
                    if l_info:
                        l_var, l_start, l_end, l_step = l_info
                        idx_expr = symbolic.pystr_to_symbolic(l_var) - symbolic.pystr_to_symbolic(l_start)
                
                if idx_expr is None and loose_mode == 'B':
                    idx_expr = symbolic.pystr_to_symbolic(info['end']) - itervar_offset
                
                if idx_expr is None:
                    # If we still don't have an index, and we are not in Pattern B, 
                    # we must be in force mode or Pattern A. Use 0 or skip?
                    # For Pattern A (loose before loops), we use index 0.
                    idx_expr = sp.Integer(0)

                new_range = sbs.Range([(idx_expr, idx_expr, 1)])

                from dace.sdfg.replace import replace_properties_dict
                
                for node, _ in state.all_nodes_recursive():
                    if isinstance(node, nd.AccessNode) and node.data == current_name:
                        node.data = ext_name
                    
                    if isinstance(node, nd.Tasklet):
                        replace_properties_dict(node, {current_name: ext_name + f"[{idx_expr}]"})
                    elif isinstance(node, nd.NestedSDFG):
                        if current_name in node.in_connectors:
                            node.add_in_connector(ext_name)
                            for e in state.in_edges(node):
                                if e.dst_conn == current_name: e.dst_conn = ext_name
                            node.remove_in_connector(current_name)
                        if current_name in node.out_connectors:
                            node.add_out_connector(ext_name)
                            for e in state.out_edges(node):
                                if e.src_conn == current_name: e.src_conn = ext_name
                            node.remove_out_connector(current_name)
                        if current_name in node.sdfg.arrays:
                            node.sdfg.arrays[ext_name] = copy.deepcopy(node.sdfg.arrays[current_name])
                            del node.sdfg.arrays[current_name]
                            node.sdfg.replace(current_name, ext_name)

                for edge, _ in state.all_edges_recursive():
                    if isinstance(edge.data, dace.memlet.Memlet):
                        if edge.data.data == current_name:
                            edge.data.data = ext_name
                            edge.data.subset = new_range
                            if edge.data.other_subset is not None:
                                edge.data.other_subset = new_range
                    elif hasattr(edge.data, 'replace'):
                        edge.data.replace(current_name, ext_name + f"[{idx_expr}]")

            expanded += 1

    print(f"Scalar expansion: {expanded} scalars expanded"
          f" ({skipped} skipped)")
    return expanded


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scalar expansion for LoopToMap unblocking")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.input)
    expand_scalars(sdfg)

    out_path = args.output or args.input.replace(".sdfgz", "_expanded.sdfgz")
    sdfg.save(out_path)
    print(f"Saved to {out_path}")
