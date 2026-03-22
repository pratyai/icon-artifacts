import dace
import pytest
from ssa.state_fusion import fuse_all_states

def test_state_fusion_basic():
    sdfg = dace.SDFG("basic_fusion")
    sdfg.add_scalar("x", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    t1 = s1.add_tasklet("t1", {}, {"out"}, "out = 1.0")
    a1 = s1.add_access("x")
    s1.add_edge(t1, "out", a1, None, dace.Memlet("x"))
    
    a2 = s2.add_read("x")
    t2 = s2.add_tasklet("t2", {"inp"}, {}, "print(inp)")
    s2.add_edge(a2, None, t2, "inp", dace.Memlet("x"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    
    new_state = sdfg.states()[0]
    access_nodes = [n for n in new_state.nodes() if isinstance(n, dace.nodes.AccessNode)]
    assert len(access_nodes) == 1
    
    sdfg.validate()

def test_state_fusion_waw_hazard():
    """Test fusion where sequentiality is preserved (via promotion for WAW)."""
    sdfg = dace.SDFG("waw_hazard")
    sdfg.add_scalar("x", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    t1 = s1.add_tasklet("t1", {}, {"out"}, "out = 1.0")
    a1 = s1.add_write("x")
    s1.add_edge(t1, "out", a1, None, dace.Memlet("x"))
    
    t2 = s2.add_tasklet("t2", {}, {"out"}, "out = 2.0")
    a2 = s2.add_write("x")
    s2.add_edge(t2, "out", a2, None, dace.Memlet("x"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    
    new_state = sdfg.states()[0]
    import networkx as nx
    assert nx.has_path(new_state.nx, t1, t2)
    
    sdfg.validate()

def test_state_fusion_war_hazard():
    """Test fusion where sequentiality is preserved (via promotion for WAR)."""
    sdfg = dace.SDFG("war_hazard")
    sdfg.add_scalar("x", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    a1 = s1.add_read("x")
    t1 = s1.add_tasklet("t1", {"inp"}, {}, "print(inp)")
    s1.add_edge(a1, None, t1, "inp", dace.Memlet("x"))
    
    t2 = s2.add_tasklet("t2", {}, {"out"}, "out = 2.0")
    a2 = s2.add_write("x")
    s2.add_edge(t2, "out", a2, None, dace.Memlet("x"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    
    new_state = sdfg.states()[0]
    import networkx as nx
    assert nx.has_path(new_state.nx, t1, t2)
    
    sdfg.validate()

def test_state_fusion_with_assignments():
    """Test fusion where an assignment exists on the edge between S1 and S2."""
    sdfg = dace.SDFG("assignments")
    sdfg.add_symbol("i", dace.int32)
    
    start = sdfg.add_state("start")
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    
    sdfg.add_edge(start, s1, dace.InterstateEdge(assignments={"i": "0"}))
    sdfg.add_edge(s1, s2, dace.InterstateEdge(assignments={"i": "5"}))
    
    s1.add_tasklet("t1", {}, {}, "print('s1')")
    s2.add_tasklet("t2", {}, {}, "print('s2')")
    
    fuse_all_states(sdfg)
    
    assert len(sdfg.nodes()) == 2
    assert s2 not in sdfg.nodes()
    
    edge = sdfg.out_edges(start)[0]
    assert edge.data.assignments["i"] == "5"
    
    sdfg.validate()

def test_state_fusion_blocked_by_data_dependency():
    """S1 writes to a symbol that is then used in an assignment on the exit edge."""
    sdfg = dace.SDFG("blocked_dep")
    sdfg.add_symbol("j", dace.int32)
    
    s1 = sdfg.add_state("s1", is_start_block=True)
    s2 = sdfg.add_state("s2")
    
    sdfg.add_scalar("i_scalar", dace.int32, transient=True)
    a1 = s1.add_write("i_scalar")
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o=1")
    s1.add_edge(t1, "o", a1, None, dace.Memlet("i_scalar"))
    
    start = sdfg.add_state("start")
    sdfg.add_edge(start, s1, dace.InterstateEdge())
    sdfg.add_edge(s1, s2, dace.InterstateEdge(assignments={"j": "i_scalar"}))
    
    fuse_all_states(sdfg)
    assert len(sdfg.nodes()) == 2
    assert s2 in sdfg.nodes()

def test_state_fusion_blocked_by_symbol_usage():
    """S1 uses a symbol in its dataflow that is modified on its exit edge."""
    sdfg = dace.SDFG("blocked_usage")
    sdfg.add_symbol("i", dace.int32)
    
    start = sdfg.add_state("start")
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    
    sdfg.add_edge(start, s1, dace.InterstateEdge())
    s1.add_tasklet("t1", {}, {}, "print(i)")
    sdfg.add_edge(s1, s2, dace.InterstateEdge(assignments={"i": "5"}))
    
    fuse_all_states(sdfg)
    assert len(sdfg.nodes()) == 2
    assert s2 in sdfg.nodes()

def test_state_fusion_blocked_by_start_state_asgn():
    """Start state has assignment on exit but no predecessors to absorb it."""
    sdfg = dace.SDFG("blocked_start")
    sdfg.add_symbol("i", dace.int32)
    s1 = sdfg.add_state("s1", is_start_block=True)
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge(assignments={"i": "5"}))
    
    fuse_all_states(sdfg)
    assert len(sdfg.nodes()) == 2

def test_state_fusion_blocked_by_non_state():
    """Cannot fuse a State with a LoopRegion (different types)."""
    sdfg = dace.SDFG("blocked_type")
    s1 = sdfg.add_state("s1", is_start_block=True)
    
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop)
    sdfg.add_edge(s1, loop, dace.InterstateEdge())
    
    fuse_all_states(sdfg)
    assert len(sdfg.nodes()) == 2

def test_state_fusion_with_nested_sdfg():
    """Test fusion where states contain NestedSDFGs."""
    sdfg = dace.SDFG("nested_fusion")
    sdfg.add_scalar("x", dace.float64, transient=True)
    
    start = sdfg.add_state("start")
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(start, s1, dace.InterstateEdge())
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    ns1 = dace.SDFG("ns1")
    ns1.add_scalar("val", dace.float64)
    nst1 = ns1.add_state()
    nt1 = nst1.add_tasklet("t", {}, {"o"}, "o = 1.0")
    nw1 = nst1.add_write("val")
    nst1.add_edge(nt1, "o", nw1, None, dace.Memlet("val"))
    
    ns2 = dace.SDFG("ns2")
    ns2.add_scalar("val", dace.float64)
    nst2 = ns2.add_state()
    nt2 = nst2.add_tasklet("t", {}, {"o"}, "o = 2.0")
    nw2 = nst2.add_write("val")
    nst2.add_edge(nt2, "o", nw2, None, dace.Memlet("val"))
    
    nnode1 = s1.add_nested_sdfg(sdfg=ns1, inputs={}, outputs={"val"})
    a1 = s1.add_write("x")
    s1.add_edge(nnode1, "val", a1, None, dace.Memlet("x"))
    
    nnode2 = s2.add_nested_sdfg(sdfg=ns2, inputs={}, outputs={"val"})
    a2 = s2.add_write("x")
    s2.add_edge(nnode2, "val", a2, None, dace.Memlet("x"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    new_s1 = sdfg.states()[0]
    
    for n in new_s1.nodes():
        if isinstance(n, dace.nodes.NestedSDFG):
            assert n.sdfg.parent == new_s1
            
    import networkx as nx
    assert nx.has_path(new_s1.nx, nnode1, nnode2)
    
    sdfg.validate()

def test_state_fusion_with_map():
    """Test fusion where states contain Maps."""
    sdfg = dace.SDFG("map_fusion")
    sdfg.add_array("A", [10], dace.float64, transient=True)
    sdfg.add_array("B", [10], dace.float64, transient=True)
    
    start = sdfg.add_state("start")
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(start, s1, dace.InterstateEdge())
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    me1, mx1 = s1.add_map("map1", {"i": "0:10"})
    t1 = s1.add_tasklet("t1", {"inp"}, {"o"}, "o = 1.0")
    a1 = s1.add_write("A")
    b1 = s1.add_read("B")
    me1.add_in_connector("IN_1")
    me1.add_out_connector("OUT_1")
    mx1.add_in_connector("IN_1")
    mx1.add_out_connector("OUT_1")
    s1.add_edge(b1, None, me1, "IN_1", dace.Memlet("B[i]"))
    s1.add_edge(me1, "OUT_1", t1, "inp", dace.Memlet("B[i]"))
    s1.add_edge(t1, "o", mx1, "IN_1", dace.Memlet("A[i]"))
    s1.add_edge(mx1, "OUT_1", a1, None, dace.Memlet("A"))
    
    me2, mx2 = s2.add_map("map2", {"i": "0:10"})
    a2 = s2.add_read("A")
    t2 = s2.add_tasklet("t2", {"inp"}, {"out"}, "out = inp")
    a3 = s2.add_write("B")
    me2.add_in_connector("IN_1")
    me2.add_out_connector("OUT_1")
    mx2.add_in_connector("IN_1")
    mx2.add_out_connector("OUT_1")
    s2.add_edge(a2, None, me2, "IN_1", dace.Memlet("A[i]"))
    s2.add_edge(me2, "OUT_1", t2, "inp", dace.Memlet("A[i]"))
    s2.add_edge(t2, "out", mx2, "IN_1", dace.Memlet("B[i]"))
    s2.add_edge(mx2, "OUT_1", a3, None, dace.Memlet("B"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    new_s1 = sdfg.states()[0]
    
    import networkx as nx
    assert nx.has_path(new_s1.nx, t1, t2)
    
    sdfg.validate()

def test_state_fusion_floating():
    """Test fusion where states touch disjoint data. They should remain parallel."""
    sdfg = dace.SDFG("floating")
    sdfg.add_scalar("x", dace.float64, transient=True)
    sdfg.add_scalar("y", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o=1")
    s1.add_edge(t1, "o", s1.add_write("x"), None, dace.Memlet("x"))
    
    t2 = s2.add_tasklet("t2", {}, {"o"}, "o=2")
    s2.add_edge(t2, "o", s2.add_write("y"), None, dace.Memlet("y"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    new_s = sdfg.states()[0]
    
    import networkx as nx
    assert not nx.has_path(new_s.nx, t1, t2)
    assert not nx.has_path(new_s.nx, t2, t1)
    
    sdfg.validate()

def test_state_fusion_multiple_anchors():
    """Test fusion where a variable has multiple sink/source points in each state."""
    sdfg = dace.SDFG("multi_anchors")
    sdfg.add_scalar("x", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    t1a = s1.add_tasklet("t1a", {}, {"o"}, "o=1")
    t1b = s1.add_tasklet("t1b", {}, {"o"}, "o=2")
    a1a = s1.add_write("x")
    a1b = s1.add_write("x")
    s1.add_edge(t1a, "o", a1a, None, dace.Memlet("x"))
    s1.add_edge(t1b, "o", a1b, None, dace.Memlet("x"))
    
    a2a = s2.add_read("x")
    a2b = s2.add_read("x")
    t2a = s2.add_tasklet("t2a", {"i"}, {}, "print(i)")
    t2b = s2.add_tasklet("t2b", {"i"}, {}, "print(i)")
    s2.add_edge(a2a, None, t2a, "i", dace.Memlet("x"))
    s2.add_edge(a2b, None, t2b, "i", dace.Memlet("x"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    new_s = sdfg.states()[0]
    
    import networkx as nx
    for src in [t1a, t1b]:
        for dst in [t2a, t2b]:
            assert nx.has_path(new_s.nx, src, dst)
            
    sdfg.validate()

def test_state_fusion_gap_variable():
    """Test fusion where x is in S1 and S3, but not S2. Chain: S1(x) -> S2(y) -> S3(x)."""
    sdfg = dace.SDFG("gap")
    sdfg.add_scalar("x", dace.float64, transient=True)
    sdfg.add_scalar("y", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    s3 = sdfg.add_state("s3")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    sdfg.add_edge(s2, s3, dace.InterstateEdge())
    
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o=1")
    s1.add_edge(t1, "o", s1.add_write("x"), None, dace.Memlet("x"))
    
    t2 = s2.add_tasklet("t2", {}, {"o"}, "o=2")
    s2.add_edge(t2, "o", s2.add_write("y"), None, dace.Memlet("y"))
    
    t3 = s3.add_tasklet("t3", {"i"}, {}, "print(i)")
    s3.add_edge(s3.add_read("x"), None, t3, "i", dace.Memlet("x"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    new_s = sdfg.states()[0]
    
    import networkx as nx
    assert nx.has_path(new_s.nx, t1, t3)
    
    sdfg.validate()

def test_state_fusion_blind_write():
    """Test fusion where S2 has a blind write (source Tasklet). 
    It should be promoted by adding a source AccessNode and merging it with S1's sink.
    """
    sdfg = dace.SDFG("blind_write")
    sdfg.add_scalar("x", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    # S1: write x
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o=1")
    a1 = s1.add_write("x")
    s1.add_edge(t1, "o", a1, None, dace.Memlet("x"))
    
    # S2: blind write x (Tasklet has no input)
    t2 = s2.add_tasklet("t2", {}, {"o"}, "o=2")
    a2 = s2.add_write("x")
    s2.add_edge(t2, "o", a2, None, dace.Memlet("x"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 1
    new_s = sdfg.states()[0]
    
    import networkx as nx
    assert nx.has_path(new_s.nx, t1, t2)
    
    t2_in_edges = [e for e in new_s.in_edges(t2) if e.data.data == "x"]
    assert len(t2_in_edges) == 1
    assert isinstance(t2_in_edges[0].src, dace.nodes.AccessNode)
    
    sdfg.validate()

def test_state_fusion_parallel_maps():
    """Two maps in sequential states, same input/output, but disjoint ranges.
    They should remain parallel.
    """
    sdfg = dace.SDFG("parallel_maps")
    sdfg.add_array("A", [20], dace.float64, transient=True)
    sdfg.add_array("B", [20], dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    # Map 1: 0-10
    me1, mx1 = s1.add_map("map1", {"i": "0:10"})
    t1 = s1.add_tasklet("t1", {"inp"}, {"out"}, "out = inp")
    a1 = s1.add_read("A")
    b1 = s1.add_write("B")
    s1.add_memlet_path(a1, me1, t1, dst_conn="inp", memlet=dace.Memlet("A[i]"))
    s1.add_memlet_path(t1, mx1, b1, src_conn="out", memlet=dace.Memlet("B[i]"))
    
    # Map 2: 10-20
    me2, mx2 = s2.add_map("map2", {"i": "10:20"})
    t2 = s2.add_tasklet("t2", {"inp"}, {"out"}, "out = inp")
    a2 = s2.add_read("A")
    b2 = s2.add_write("B")
    s2.add_memlet_path(a2, me2, t2, dst_conn="inp", memlet=dace.Memlet("A[i]"))
    s2.add_memlet_path(t2, mx2, b2, src_conn="out", memlet=dace.Memlet("B[i]"))
    
    fuse_all_states(sdfg)
    
    assert len(sdfg.states()) == 1
    new_s = sdfg.states()[0]
    
    import networkx as nx
    # They should be parallel (no path) because ranges are disjoint
    assert not nx.has_path(new_s.nx, t1, t2)
    assert not nx.has_path(new_s.nx, t2, t1)
    
    sdfg.validate()

def test_state_fusion_blocked_by_cfg_split():
    """S1 has two outgoing edges (a split). Cannot fuse S1->S2."""
    sdfg = dace.SDFG("cfg_split")
    sdfg.add_symbol("i", dace.int32)
    s1 = sdfg.add_state("s1", is_start_block=True)
    s2 = sdfg.add_state("s2")
    s3 = sdfg.add_state("s3")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    sdfg.add_edge(s1, s3, dace.InterstateEdge(condition="i > 0"))
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 3

def test_state_fusion_blocked_by_cfg_join():
    """S3 has two incoming edges (a join). Cannot fuse S1->S3."""
    sdfg = dace.SDFG("cfg_join")
    s1 = sdfg.add_state("s1", is_start_block=True)
    s2 = sdfg.add_state("s2")
    s3 = sdfg.add_state("s3")
    sdfg.add_edge(s1, s3, dace.InterstateEdge())
    sdfg.add_edge(s2, s3, dace.InterstateEdge())
    
    fuse_all_states(sdfg)
    assert len(sdfg.states()) == 3

if __name__ == "__main__":
    test_state_fusion_basic()
    test_state_fusion_waw_hazard()
    test_state_fusion_war_hazard()
    test_state_fusion_with_assignments()
    test_state_fusion_blocked_by_data_dependency()
    test_state_fusion_blocked_by_symbol_usage()
    test_state_fusion_blocked_by_start_state_asgn()
    test_state_fusion_blocked_by_non_state()
    test_state_fusion_with_nested_sdfg()
    test_state_fusion_with_map()
    test_state_fusion_floating()
    test_state_fusion_multiple_anchors()
    test_state_fusion_gap_variable()
    test_state_fusion_blind_write()
    test_state_fusion_parallel_maps()
    test_state_fusion_blocked_by_cfg_split()
    test_state_fusion_blocked_by_cfg_join()
    print("All tests passed!")
