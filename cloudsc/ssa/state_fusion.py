"""state_fusion.py — Ground-up State Fusion using the Access Chain model.

Logic:
1. Identify a linear chain S1 -> S2.
2. Identify all shared data accessed in both states.
3. For each shared variable:
   - Identify top-level AccessNodes that act as "ends" (sinks) in S1 or "starts" (sources) in S2.
   - If a computational node (Tasklet/Map/NestedSDFG) is a boundary point, "promote" it
     by adding a top-level AccessNode and connecting it.
   - UNIFY all S1 sinks for variable 'A' into a single 'master sink' AccessNode.
   - UNIFY all S2 sources for variable 'A' into a single 'master source' AccessNode.
   - Stitch the master sink to the master source by merging them.
4. Mechanical execution of the plan.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple, Any, Optional
import networkx as nx

import dace
from dace import nodes, subsets, memlet
from dace.sdfg import utils as sdutil
from dace.sdfg.state import SDFGState, ControlFlowRegion


@dataclass
class LinearFusionPlan:
    edge: Any
    s1: SDFGState
    s2: SDFGState
    # (src_proxy, snk_proxy) for merging. MERGE src INTO snk.
    merges: List[Tuple[Any, Any]] = field(default_factory=list)
    # (state, computational_node, varname, is_sink)
    promotions: List[Tuple[SDFGState, nodes.Node, str, bool]] = field(default_factory=list)


def fuse_all_states(sdfg: dace.SDFG):
    """Recursively find and apply linear fusion plans."""
    from tqdm import tqdm
    pbar = tqdm(desc="Fusing states", unit="fusion")
    
    applied_total = 0
    while True:
        plan = None
        for current_sdfg in sdfg.all_sdfgs_recursive():
            for cfg in current_sdfg.all_control_flow_regions():
                plan = _find_linear_plan(cfg)
                if plan:
                    _execute_linear_fusion(current_sdfg, cfg, plan)
                    applied_total += 1
                    pbar.update(1)
                    break
            if plan: break
        if not plan: break
    
    pbar.close()
    if applied_total > 0:
        print(f"State fusion: {applied_total} states fused")


def _find_linear_plan(cfg: ControlFlowRegion) -> Optional[LinearFusionPlan]:
    for edge in cfg.edges():
        s1, s2 = edge.src, edge.dst
        if not isinstance(s1, SDFGState) or not isinstance(s2, SDFGState):
            continue
        
        # 1. Structural Check
        if cfg.out_degree(s1) != 1 or cfg.in_degree(s2) != 1: continue
        if not edge.data.is_unconditional(): continue
        
        if edge.data.assignments:
            in_edges = cfg.in_edges(s1)
            if not in_edges: continue 
            new_asgn = set(edge.data.assignments.keys())
            s1_syms = set()
            for n in s1.nodes(): s1_syms.update(str(s) for s in n.free_symbols)
            if new_asgn & s1_syms: continue
            freesyms = edge.data.free_symbols
            s1_written = set()
            for n in s1.nodes():
                for e in s1.out_edges(n):
                    if e.data.data: s1_written.add(e.data.data)
            if freesyms & s1_written: continue

        # 2. Access Chain Analysis
        plan = LinearFusionPlan(edge=edge, s1=s1, s2=s2)
        
        def get_all_data(state: SDFGState):
            res = set()
            for n in state.all_nodes_recursive():
                if isinstance(n, nodes.AccessNode): res.add(n.data)
            return res

        shared_data = get_all_data(s1) & get_all_data(s2)
        
        for data in shared_data:
            def get_boundaries(state: SDFGState, varname: str, outgoing: bool):
                boundary = []
                for n in state.nodes():
                    if state.scope_dict()[n] is not None: continue
                    touches = False
                    if isinstance(n, nodes.AccessNode) and n.data == varname: touches = True
                    else:
                        for e in state.all_edges(n):
                            if e.data.data == varname: touches = True; break
                    if touches:
                        if outgoing:
                            if not any(e.data.data == varname for e in state.out_edges(n)):
                                boundary.append(n)
                        else:
                            if not any(e.data.data == varname for e in state.in_edges(n)):
                                boundary.append(n)
                return boundary

            s1_sinks = get_boundaries(s1, data, outgoing=True)
            s2_sources = get_boundaries(s2, data, outgoing=False)
            
            if s1_sinks and s2_sources:
                if not _memlets_intersect(s1, s1_sinks, s2, s2_sources):
                    continue

                # UNIFY boundary nodes into anchors
                s1_anchor = None
                for n in s1_sinks:
                    if isinstance(n, nodes.AccessNode):
                        if s1_anchor is None: s1_anchor = n
                        else: plan.merges.append((n, s1_anchor))
                    else:
                        proxy = (n, data, True)
                        plan.promotions.append((s1, n, data, True))
                        if s1_anchor is None: s1_anchor = proxy
                        else: plan.merges.append((proxy, s1_anchor))

                s2_anchor = None
                for n in s2_sources:
                    if isinstance(n, nodes.AccessNode):
                        if s2_anchor is None: s2_anchor = n
                        else: plan.merges.append((n, s2_anchor))
                    else:
                        proxy = (n, data, False)
                        plan.promotions.append((s2, n, data, False))
                        if s2_anchor is None: s2_anchor = proxy
                        else: plan.merges.append((proxy, s2_anchor))

                plan.merges.append((s2_anchor, s1_anchor))

        return plan
    return None


def _execute_linear_fusion(sdfg: dace.SDFG, cfg: ControlFlowRegion, plan: LinearFusionPlan):
    s1, s2 = plan.s1, plan.s2
    
    if plan.edge.data.assignments:
        for in_e in cfg.in_edges(s1):
            in_e.data.assignments.update(plan.edge.data.assignments)

    s2_node_map = {}
    for n2 in s2.nodes():
        if isinstance(n2, nodes.NestedSDFG): n2.sdfg.parent = s1
        s1.add_node(n2)
        s2_node_map[n2] = n2
            
    promo_map = {}
    for state, n, varname, is_sink in plan.promotions:
        new_an = s1.add_access(varname)
        actual_n = s2_node_map[n] if state == s2 else n
        if is_sink:
            s1.add_nedge(actual_n, new_an, dace.Memlet.from_array(varname, sdfg.arrays[varname]))
        else:
            s1.add_nedge(new_an, actual_n, dace.Memlet.from_array(varname, sdfg.arrays[varname]))
        promo_map[(n, varname, is_sink)] = new_an

    final_node_map = {}
    def resolve(proxy_or_node):
        if isinstance(proxy_or_node, tuple): return promo_map[proxy_or_node]
        if proxy_or_node in s2_node_map: return s2_node_map[proxy_or_node]
        return proxy_or_node

    for src_p, target_p in plan.merges:
        n_src = resolve(src_p)
        n_target = resolve(target_p)
        
        if n_src in s1.nodes() and n_target in s1.nodes() and n_src != n_target:
            if not nx.has_path(s1.nx, n_target, n_src):
                # Filter edges that would become self-loops
                for e in list(s1.all_edges(n_src)):
                    if e.src == n_src and e.dst == n_target: s1.remove_edge(e)
                    elif e.src == n_target and e.dst == n_src: s1.remove_edge(e)
                
                if n_src in s1.nodes():
                    sdutil.change_edge_src(s1, n_src, n_target)
                    sdutil.change_edge_dest(s1, n_src, n_target)
                    s1.remove_node(n_src)
                    if not isinstance(src_p, tuple): final_node_map[src_p] = n_target

    for n2 in s2.nodes():
        if n2 not in final_node_map: final_node_map[n2] = s2_node_map[n2]

    for src, src_conn, dst, dst_conn, data in s2.edges():
        s1.add_edge(final_node_map[src], src_conn, final_node_map[dst], dst_conn, data)
        
    cfg.remove_edge(plan.edge)
    sdutil.change_edge_src(cfg, s2, s1)
    cfg.remove_node(s2)
    if cfg.start_block == s2: cfg.start_block = cfg.node_id(s1)
    sdfg.reset_cfg_list()


def _memlets_intersect(s1: SDFGState, g1: List[nodes.Node], s2: SDFGState, g2: List[nodes.Node]) -> bool:
    edges1 = [e for n in g1 for e in s1.all_edges(n)]
    edges2 = [e for n in g2 for e in s2.all_edges(n)]
    for e1 in edges1:
        if e1.data.is_empty(): continue
        for e2 in edges2:
            if e2.data.is_empty(): continue
            if e1.data.data != e2.data.data: continue
            s1_sub = e1.data.subset or subsets.Range.from_array(s1.sdfg.arrays[e1.data.data])
            s2_sub = e2.data.subset or subsets.Range.from_array(s2.sdfg.arrays[e2.data.data])
            res = subsets.intersects(s1_sub, s2_sub)
            if res is True or res is None: return True
    return False
