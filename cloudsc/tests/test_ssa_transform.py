import dace
import pytest
import re
from ssa.data_ssa import ssa_transform

def test_ssa_transform_sequential():
    sdfg = dace.SDFG("test_seq")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    state1 = sdfg.add_state("s1")
    t1 = state1.add_tasklet("t1", {}, {"out"}, "out = 1.0")
    state1.add_edge(t1, "out", state1.add_write("s"), None, dace.Memlet("s[0]"))
    
    state2 = sdfg.add_state("s2")
    sdfg.add_edge(state1, state2, dace.InterstateEdge())
    t2 = state2.add_tasklet("t2", {"in1"}, {"out1"}, "out1 = in1 + 1.0")
    state2.add_edge(state2.add_read("s"), None, t2, "in1", dace.Memlet("s[0]"))
    state2.add_edge(t2, "out1", state2.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Run SSA
    res = ssa_transform(sdfg)
    
    assert "s" in res
    assert len(res["s"]) == 2
    v1, v2 = res["s"]
    
    # Check that s2 reads v1 and writes v2
    s2_reads = [n.data for n in state2.nodes() if isinstance(n, dace.nodes.AccessNode) and state2.out_degree(n) > 0]
    s2_writes = [n.data for n in state2.nodes() if isinstance(n, dace.nodes.AccessNode) and state2.in_degree(n) > 0]
    assert v1 in s2_reads
    assert v2 in s2_writes
    sdfg.validate()

def test_ssa_transform_conditional():
    sdfg = dace.SDFG("test_cond")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # Initial write
    init = sdfg.add_state("init")
    init.add_edge(init.add_tasklet("t", {}, {"o"}, "o=0"), "o", init.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Conditional
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())
    
    # Branch 1: writes s
    b1 = dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg)
    s1 = b1.add_state("s1")
    s1.add_edge(s1.add_tasklet("t", {}, {"o"}, "o=1"), "o", s1.add_write("s"), None, dace.Memlet("s[0]"))
    cb.add_branch("True", b1)
    
    # Branch 2 (else): writes s (different version)
    b2 = dace.sdfg.state.ControlFlowRegion("b2", sdfg=sdfg)
    s2 = b2.add_state("s2")
    s2.add_edge(s2.add_tasklet("t", {}, {"o"}, "o=2"), "o", s2.add_write("s"), None, dace.Memlet("s[0]"))
    cb.add_branch(None, b2)

    # Post-conditional read
    after = sdfg.add_state("after")
    sdfg.add_edge(cb, after, dace.InterstateEdge())
    t_after = after.add_tasklet("t_after", {"in1"}, {}, "x = in1")
    after.add_edge(after.add_read("s"), None, t_after, "in1", dace.Memlet("s[0]"))
    
    # Run SSA. Both branches wrote, so after-read gets the merged branch version (v2),
    # not the pre-conditional version (v1).
    ssa_transform(sdfg)

    after_node = [n for n in after.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    assert "__v2" in after_node.data
    sdfg.validate()

def test_ssa_transform_loop_carried():
    sdfg = dace.SDFG("test_loop")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # Loop where s is read then written
    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("ls")
    t = ls.add_tasklet("t", {"i"}, {"o"}, "o = i + 1")
    ls.add_edge(ls.add_read("s"), None, t, "i", dace.Memlet("s[0]"))
    ls.add_edge(t, "o", ls.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Run SSA. Should detect carry-over and REVERT renaming inside loop.
    res = ssa_transform(sdfg)
    
    # 's' should NOT be in result because it should have been reverted
    assert "s" not in res
    node = [n for n in ls.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    assert node.data == "s"
    sdfg.validate()

def test_ssa_transform_interstate_edges():
    sdfg = dace.SDFG("test_edges")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s1.add_edge(s1.add_tasklet("t", {}, {"o"}, "o=1"), "o", s1.add_write("s"), None, dace.Memlet("s[0]"))
    
    s2 = sdfg.add_state("s2")
    # Edge using 's' in condition
    edge = sdfg.add_edge(s1, s2, dace.InterstateEdge(condition="s > 0"))
    
    # Force SSA on 's' (since it only has 1 write)
    ssa_transform(sdfg, only={"s"})
    
    # Condition should use SSA name
    assert "s__v1" in edge.data.condition.as_string
    sdfg.validate()

def test_ssa_transform_complex_metadata():
    sdfg = dace.SDFG("test_metadata")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # 1. Initial write
    s1 = sdfg.add_state("s1")
    s1.add_edge(s1.add_tasklet("t", {}, {"o"}, "o=1"), "o", s1.add_write("s"), None, dace.Memlet("s[0]"))
    
    # 2. LoopRegion using 's' in condition
    loop = dace.sdfg.state.LoopRegion("l", "i < s", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    sdfg.add_edge(s1, loop, dace.InterstateEdge())
    ls = loop.add_state("ls")
    
    # 3. ConditionalBlock using 's' in branch
    cb = dace.sdfg.state.ConditionalBlock("cb")
    loop.add_node(cb)
    loop.add_edge(ls, cb, dace.InterstateEdge())
    
    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("s > 0", branch)
    
    # 4. Interstate edge assignment
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(loop, s2, dace.InterstateEdge(assignments={"x": "s + 1"}))
    
    # Force SSA on 's' (1 write)
    ssa_transform(sdfg, only={"s"})
    
    # Assertions
    # Loop condition
    assert "s__v1" in loop.loop_condition.as_string
    # Conditional branch
    assert "s__v1" in cb.branches[0][0].as_string
    # Interstate edge assignment
    edge = sdfg.edges_between(loop, s2)[0].data
    assert "s__v1" in edge.assignments["x"]
    
    sdfg.validate()

def test_ssa_nested_sdfg_mapping():
    sdfg = dace.SDFG("parent")
    # Scalar written twice to trigger SSA
    sdfg.add_scalar("s_scalar", dace.float64, transient=True)
    
    state = sdfg.add_state("s1")
    # First write
    t1 = state.add_tasklet("t1", {}, {"out"}, "out = 1.0")
    state.add_edge(t1, "out", state.add_write("s_scalar"), None, dace.Memlet("s_scalar[0]"))
    
    # Second write in another state
    state2 = sdfg.add_state("s2")
    sdfg.add_edge(state, state2, dace.InterstateEdge())
    
    nsdfg = dace.SDFG("child")
    nsdfg.add_symbol("s_internal", dace.float64)
    # Use the symbol in child
    cs = nsdfg.add_state("cs")
    cs.add_tasklet("ct", {}, {}, "x = s_internal")
    
    # Nested SDFG node in parent mapping 's_scalar' to 's_internal'
    nnode = state2.add_nested_sdfg(nsdfg, set(), set(), {"s_internal": dace.symbol("s_scalar")})
    
    t2 = state2.add_tasklet("t2", {}, {"out"}, "out = 2.0")
    state2.add_edge(t2, "out", state2.add_write("s_scalar"), None, dace.Memlet("s_scalar[0]"))
    
    # Run SSA
    ssa_transform(sdfg)
    
    # Validation check
    sdfg.validate()
    
    # Verify mapping was updated to one of the versions
    new_val = str(nnode.symbol_mapping["s_internal"])
    assert "s_scalar__v" in new_val

def test_ssa_transform_map():
    sdfg = dace.SDFG("test_map")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # State 1: initial write
    s1 = sdfg.add_state("s1")
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o = 1.0")
    s1.add_edge(t1, "o", s1.add_write("s"), None, dace.Memlet("s[0]"))
    
    # State 2: Map reading and writing s
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    me, mx = s2.add_map("m", dict(i="0:10"))
    t2 = s2.add_tasklet("t2", {"in1"}, {"out1"}, "out1 = in1")
    
    ani = s2.add_access("s")
    ano = s2.add_access("s")
    s2.add_memlet_path(ani, me, t2, dst_conn="in1", memlet=dace.Memlet("s[0]"))
    s2.add_memlet_path(t2, mx, ano, src_conn="out1", memlet=dace.Memlet("s[0]"))
    
    # SSA should version 's' into s__v1 (s1 write) and s__v2 (s2 map write)
    ssa_transform(sdfg)
    
    # Check map connectors
    assert "IN_s__v1" in me.in_connectors
    assert "OUT_s__v1" in me.out_connectors
    assert "IN_s__v2" in mx.in_connectors
    assert "OUT_s__v2" in mx.out_connectors
    
    sdfg.validate()

def test_ssa_transform_cleanup():
    sdfg = dace.SDFG("test_cleanup")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # 1. Initial write (v1)
    init = sdfg.add_state("init")
    init.add_edge(init.add_tasklet("t1", {}, {"o"}, "o=0"), "o", init.add_write("s"), None, dace.Memlet("s[0]"))
    
    # 2. Conditional where branches disagree (v2, v3 will be created then overridden by v1)
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())
    
    b1 = dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg)
    s1 = b1.add_state("s1")
    s1.add_edge(s1.add_tasklet("t2", {}, {"o"}, "o=1"), "o", s1.add_write("s"), None, dace.Memlet("s[0]"))
    cb.add_branch("True", b1)
    
    b2 = dace.sdfg.state.ControlFlowRegion("b2", sdfg=sdfg)
    s2 = b2.add_state("s2")
    s2.add_edge(s2.add_tasklet("t3", {}, {"o"}, "o=2"), "o", s2.add_write("s"), None, dace.Memlet("s[0]"))
    cb.add_branch(None, b2)

    # 3. Post-conditional read
    after = sdfg.add_state("after")
    sdfg.add_edge(cb, after, dace.InterstateEdge())
    after.add_edge(after.add_read("s"), None, after.add_tasklet("t4", {"i"}, {}, "x=i"), "i", dace.Memlet("s[0]"))
    
    # Run SSA
    ssa_transform(sdfg)
    
    # v1 (init write) and v2 (merged branch writes + after read). v3 should be GONE.
    v_names = [name for name in sdfg.arrays if "s__v" in name]
    assert len(v_names) == 2
    assert "s__v1" in v_names
    assert "s__v2" in v_names
    assert "s__v3" not in sdfg.arrays

def test_ssa_transform_cleanup_metadata():
    sdfg = dace.SDFG("test_cleanup_metadata")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # 1. Write (v1)
    s1 = sdfg.add_state("s1")
    s1.add_edge(s1.add_tasklet("t1", {}, {"o"}, "o=1"), "o", s1.add_write("s"), None, dace.Memlet("s[0]"))
    
    # 2. Use ONLY in metadata (Loop bound)
    loop = dace.sdfg.state.LoopRegion("l", "i < s", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    sdfg.add_edge(s1, loop, dace.InterstateEdge())
    
    # 3. Another write (v2) after the loop
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(loop, s2, dace.InterstateEdge())
    s2.add_edge(s2.add_tasklet("t2", {}, {"o"}, "o=2"), "o", s2.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Run SSA
    ssa_transform(sdfg)
    
    # Both v1 and v2 should exist. v1 is used in LoopRegion.loop_condition metadata.
    assert "s__v1" in sdfg.arrays
    assert "s__v2" in sdfg.arrays
    assert "s__v1" in loop.loop_condition.as_string

def test_ssa_transform_double_write_conditional():
    sdfg = dace.SDFG("test_double_write")
    sdfg.add_scalar("x", dace.float64, transient=True)
    
    # State 1: Write 1
    s1 = sdfg.add_state("s1")
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o=1")
    an1 = s1.add_write("x")
    s1.add_edge(t1, "o", an1, None, dace.Memlet("x[0]"))
    
    # State 2: Read 1 & Write 2
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    an_r = s2.add_read("x")
    t2 = s2.add_tasklet("t2", {"i"}, {"o"}, "o=i+1")
    an2 = s2.add_write("x")
    s2.add_edge(an_r, None, t2, "i", dace.Memlet("x[0]"))
    s2.add_edge(t2, "o", an2, None, dace.Memlet("x[0]"))
    
    # Conditional Block
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(s2, cb, dace.InterstateEdge())
    
    # Branch reading x
    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("x > 0", branch)
    
    # Run SSA
    ssa_transform(sdfg)
    
    # Check that x was versioned
    # an1 (write) -> x__v1
    # an_r (read)  -> x__v1
    # an2 (write) -> x__v2
    # cb (usage)   -> x__v2
    
    assert an1.data == "x__v1"
    assert an_r.data == "x__v1"
    assert an2.data == "x__v2"
    assert "x__v2" in cb.branches[0][0].as_string
    
    sdfg.validate()

if __name__ == "__main__":
    test_ssa_transform_sequential()
    test_ssa_transform_conditional()
    test_ssa_transform_loop_carried()
    test_ssa_transform_interstate_edges()
    test_ssa_transform_complex_metadata()
    test_ssa_nested_sdfg_mapping()
    test_ssa_transform_map()
    test_ssa_transform_cleanup()
    test_ssa_transform_cleanup_metadata()
    test_ssa_transform_double_write_conditional()
    print("All SSA transform tests passed!")
