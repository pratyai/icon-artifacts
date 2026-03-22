"""state_fusion.py — Ground-up State Fusion using the Access Chain model.

Logic:
1. Identify a linear chain S1 -> S2.
2. Identify all shared data accessed in both states.
3. For each shared variable:
   - Identify top-level AccessNodes that act as "ends" (sinks) in S1 or "starts" (sources) in S2.
   - If a computational node (Tasklet/Map/NestedSDFG) is a boundary point, "promote" it
     by adding a top-level AccessNode and connecting it.
   - Stitch the chains by merging/connecting ALL S1 sinks with ALL S2 sources.
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
    # (s2_node_proxy, s1_node_proxy) for merging
    merges: List[Tuple[Any, Any]] = field(default_factory=list)
    # promotions to perform in each state before merging
    # (state, computational_node, varname, is_sink)
    promotions: List[Tuple[SDFGState, nodes.Node, str, bool]] = field(default_factory=list)
    # (s1_node_proxy, s2_node_proxy) for happens-before edges
    dependencies: List[Tuple[Any, Any]] = field(default_factory=list)


def fuse_all_states(sdfg: dace.SDFG):
    """Recursively find and apply linear fusion plans."""
    from tqdm import tqdm
    pbar = tqdm(desc="Fusing states", unit="fusion")
    
    applied_total = 0
    while True:
        applied_in_round = False
        for current_sdfg in sdfg.all_sdfgs_recursive():
            for cfg in current_sdfg.all_control_flow_regions():
                plan = _find_linear_plan(cfg)
                if plan:
                    _execute_linear_fusion(current_sdfg, cfg, plan)
                    applied_in_round = True
                    applied_total += 1
                    pbar.update(1)
                    break 
            if applied_in_round: break
        if not applied_in_round: break
    
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
            for n in state.nodes():
                if isinstance(n, nodes.AccessNode): res.add(n.data)
                for e in state.all_edges(n):
                    if e.data.data: res.add(e.data.data)
            return res

        shared_data = get_all_data(s1) & get_all_data(s2)
        
        for data in shared_data:
            def get_boundaries(state: SDFGState, varname: str, outgoing: bool):
                nodes_touching = []
                for n in state.nodes():
                    if isinstance(n, nodes.AccessNode) and n.data == varname:
                        nodes_touching.append(n)
                    else:
                        for e in state.all_edges(n):
                            if e.data.data == varname:
                                nodes_touching.append(n); break
                
                boundary = []
                for n in nodes_touching:
                    if state.scope_dict()[n] is not None: continue
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
                # STITCHING: only if accesses actually intersect
                if _memlets_intersect(s1, s1_sinks, s2, s2_sources):
                    # Promote computational boundary nodes
                    final_s1_sinks = []
                    for n in s1_sinks:
                        if not isinstance(n, nodes.AccessNode):
                            proxy = (n, data, True)
                            plan.promotions.append((s1, n, data, True))
                            final_s1_sinks.append(proxy)
                        else:
                            final_s1_sinks.append(n)

                    final_s2_sources = []
                    for n in s2_sources:
                        if not isinstance(n, nodes.AccessNode):
                            proxy = (n, data, False)
                            plan.promotions.append((s2, n, data, False))
                            final_s2_sources.append(proxy)
                        else:
                            final_s2_sources.append(n)

                    # Stitch ALL pairs to ensure full sequentiality
                    # We pick the first pair to merge (if compatible AccessNodes),
                    # all others get explicit dependencies.
                    main_snk = final_s1_sinks[0]
                    main_src = final_s2_sources[0]
                    
                    s1_has_write = any(s1.in_degree(n) > 0 for n in s1.data_nodes() if n.data == data)
                    def is_read(state, node_proxy):
                        if isinstance(node_proxy, tuple): return False # computational
                        return state.in_degree(node_proxy) == 0
                    
                    can_merge_main = False
                    if isinstance(main_snk, nodes.AccessNode) and isinstance(main_src, nodes.AccessNode):
                        if not is_read(s1, main_snk) or (is_read(s1, main_snk) and not s1_has_write):
                            if is_read(s2, main_src): can_merge_main = True

                    if can_merge_main:
                        plan.merges.append((main_src, main_snk))
                    else:
                        plan.dependencies.append((main_snk, main_src))

                    # All other pairs get dependencies
                    for snk in final_s1_sinks:
                        for src in final_s2_sources:
                            if (snk, src) == (main_snk, main_src): continue
                            plan.dependencies.append((snk, src))

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
        return proxy_or_node

    # Apply dependencies first
    for snk_proxy, src_proxy in plan.dependencies:
        snk = resolve(snk_proxy)
        src = resolve(src_proxy)
        src_migrated = s2_node_map[src] if src in s2_node_map.values() else src
        if not nx.has_path(s1.nx, snk, src_migrated):
            s1.add_nedge(snk, src_migrated, memlet.Memlet())

    # Apply merges
    for src_proxy, snk_proxy in plan.merges:
        n1 = resolve(snk_proxy)
        n2_migrated = resolve(src_proxy)
        if n2_migrated in s1.nodes() and n1 in s1.nodes() and n1 != n2_migrated:
            sdutil.change_edge_src(s1, n2_migrated, n1)
            sdutil.change_edge_dest(s1, n2_migrated, n1)
            s1.remove_node(n2_migrated)
            if not isinstance(src_proxy, tuple):
                final_node_map[src_proxy] = n1

    for n2 in s2.nodes():
        if n2 not in final_node_map:
            final_node_map[n2] = s2_node_map[n2]

    # 5. Edge Migration
    for src, src_conn, dst, dst_conn, data in s2.edges():
        s1.add_edge(final_node_map[src], src_conn, final_node_map[dst], dst_conn, data)
        
    cfg.remove_edge(plan.edge)
    sdutil.change_edge_src(cfg, s2, s1)
    cfg.remove_node(s2)
    if cfg.start_block == s2: cfg.start_block = cfg.node_id(s1)
    sdfg.reset_cfg_list()


def _memlets_intersect(s1: SDFGState, g1: List[nodes.Node], s2: SDFGState, g2: List[nodes.Node]) -> bool:
    """Check for any intersection between memlets in two groups."""
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
