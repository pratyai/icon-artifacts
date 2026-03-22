import dace
import pytest
from ssa.ssa_analysis import SSAVersionMap, SSAPlan, analyze_cfg

def test_analyze_sequential():
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o=1")
    an1 = s1.add_write("s")
    s1.add_edge(t1, "o", an1, None, dace.Memlet("s[0]"))
    
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    an2 = s2.add_read("s")
    t2 = s2.add_tasklet("t2", {"i"}, {"o"}, "o=i")
    an3 = s2.add_write("s")
    s2.add_edge(an2, None, t2, "i", dace.Memlet("s[0]"))
    s2.add_edge(t2, "o", an3, None, dace.Memlet("s[0]"))
    
    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)
    
    # an1 (write) -> v0
    # an2 (read)  -> v0
    # an3 (write) -> v1
    assert len(plan.versions) == 2
    v0 = plan.versions[0]
    v1 = plan.versions[1]
    assert (s1, an1) in v0.access_nodes
    assert (s2, an2) in v0.access_nodes
    assert (s2, an3) in v1.access_nodes

def test_analyze_loop_carried():
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    ls = loop.add_state("ls")
    an_r = ls.add_read("s")
    t = ls.add_tasklet("t", {"i"}, {"o"}, "o=i")
    an_w = ls.add_write("s")
    ls.add_edge(an_r, None, t, "i", dace.Memlet("s[0]"))
    ls.add_edge(t, "o", an_w, None, dace.Memlet("s[0]"))
    
    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)
    
    # Loop-carried read before write -> Revert body version to original (None)
    assert len(plan.versions) == 0

def test_analyze_loop_carried_multi_state():
    """All intermediate versions must be reverted when carry is detected."""
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)

    # State 1: read s, write s (first access = RW → carry)
    ls1 = loop.add_state("ls1")
    t1 = ls1.add_tasklet("t1", {"i"}, {"o"}, "o=i")
    ls1.add_edge(ls1.add_read("s"), None, t1, "i", dace.Memlet("s[0]"))
    ls1.add_edge(t1, "o", ls1.add_write("s"), None, dace.Memlet("s[0]"))

    # State 2: read s, write s (creates intermediate version)
    ls2 = loop.add_state("ls2")
    loop.add_edge(ls1, ls2, dace.InterstateEdge())
    t2 = ls2.add_tasklet("t2", {"i"}, {"o"}, "o=i+1")
    ls2.add_edge(ls2.add_read("s"), None, t2, "i", dace.Memlet("s[0]"))
    ls2.add_edge(t2, "o", ls2.add_write("s"), None, dace.Memlet("s[0]"))

    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)

    # Carry detected, no pre-def → ALL body versions reverted
    assert len(plan.versions) == 0


def test_analyze_loop_carried_multi_state_with_pre_def():
    """All intermediate versions must merge into entry_ver when carry is detected."""
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)

    init = sdfg.add_state("init")
    an_init = init.add_write("s")
    init.add_edge(init.add_tasklet("t0", {}, {"o"}, "o=0"), "o", an_init, None, dace.Memlet("s[0]"))

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    sdfg.add_edge(init, loop, dace.InterstateEdge())

    ls1 = loop.add_state("ls1")
    t1 = ls1.add_tasklet("t1", {"i"}, {"o"}, "o=i")
    an_r1 = ls1.add_read("s")
    an_w1 = ls1.add_write("s")
    ls1.add_edge(an_r1, None, t1, "i", dace.Memlet("s[0]"))
    ls1.add_edge(t1, "o", an_w1, None, dace.Memlet("s[0]"))

    ls2 = loop.add_state("ls2")
    loop.add_edge(ls1, ls2, dace.InterstateEdge())
    t2 = ls2.add_tasklet("t2", {"i"}, {"o"}, "o=i+1")
    an_r2 = ls2.add_read("s")
    an_w2 = ls2.add_write("s")
    ls2.add_edge(an_r2, None, t2, "i", dace.Memlet("s[0]"))
    ls2.add_edge(t2, "o", an_w2, None, dace.Memlet("s[0]"))

    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)

    # Carry with pre-def → ALL body versions merged into entry_ver
    assert len(plan.versions) == 1
    v0 = plan.versions[0]
    assert (init, an_init) in v0.access_nodes
    assert (ls1, an_r1) in v0.access_nodes
    assert (ls1, an_w1) in v0.access_nodes
    assert (ls2, an_r2) in v0.access_nodes
    assert (ls2, an_w2) in v0.access_nodes


def test_analyze_loop_carried_write_node_first():
    """Regression: _first_access_in_body must scan ALL nodes, not return on first hit."""
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    ls = loop.add_state("ls")
    # Insert write node BEFORE read node to exercise iteration-order sensitivity
    an_w = ls.add_write("s")
    an_r = ls.add_read("s")
    t = ls.add_tasklet("t", {"i"}, {"o"}, "o=i")
    ls.add_edge(an_r, None, t, "i", dace.Memlet("s[0]"))
    ls.add_edge(t, "o", an_w, None, dace.Memlet("s[0]"))

    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)

    # Same expectation as test_analyze_loop_carried: loop-carried → revert
    assert len(plan.versions) == 0

def test_analyze_loop_carried_with_pre_def():
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    init = sdfg.add_state("init")
    init.add_edge(init.add_tasklet("t1", {}, {"o"}, "o=0"), "o", init.add_write("s"), None, dace.Memlet("s[0]"))
    
    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    sdfg.add_edge(init, loop, dace.InterstateEdge())
    ls = loop.add_state("ls")
    an_r = ls.add_read("s")
    an_w = ls.add_write("s")
    ls.add_edge(an_r, None, ls.add_tasklet("t2", {"i"}, {"o"}, "o=i"), "i", dace.Memlet("s[0]"))
    # (Tasklet to an_w edge)
    t2 = [n for n in ls.nodes() if isinstance(n, dace.nodes.Tasklet)][0]
    ls.add_edge(t2, "o", an_w, None, dace.Memlet("s[0]"))
    
    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)
    
    # Loop-carried -> merge body write (v1) into pre-def (v0)
    assert len(plan.versions) == 1
    v0 = plan.versions[0]
    assert (init, [n for n in init.nodes() if isinstance(n, dace.nodes.AccessNode)][0]) in v0.access_nodes
    assert (ls, an_r) in v0.access_nodes
    assert (ls, an_w) in v0.access_nodes

def test_analyze_conditional_mismatch():
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    init = sdfg.add_state("init")
    an_init = init.add_write("s")
    init.add_edge(init.add_tasklet("t0", {}, {"o"}, "o=0"), "o", an_init, None, dace.Memlet("s[0]"))
    
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())
    
    b1 = dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg)
    s1 = b1.add_state("s1")
    an1 = s1.add_write("s")
    s1.add_edge(s1.add_tasklet("t1", {}, {"o"}, "o=1"), "o", an1, None, dace.Memlet("s[0]"))
    cb.add_branch("True", b1)
    
    b2 = dace.sdfg.state.ControlFlowRegion("b2", sdfg=sdfg)
    s2 = b2.add_state("s2")
    an2 = s2.add_write("s")
    s2.add_edge(s2.add_tasklet("t2", {}, {"o"}, "o=2"), "o", an2, None, dace.Memlet("s[0]"))
    cb.add_branch(None, b2)
    
    after = sdfg.add_state("after")
    sdfg.add_edge(cb, after, dace.InterstateEdge())
    an_after = after.add_read("s")
    
    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)
    
    # Both branches wrote -> pre_ver (v0) stays separate, branch writes merge together
    assert len(plan.versions) == 2
    # Find which version has the init write vs the branch writes
    v_init = next(v for v in plan.versions if (init, an_init) in v.access_nodes)
    v_branch = next(v for v in plan.versions if (s1, an1) in v.access_nodes)
    assert (s2, an2) in v_branch.access_nodes
    assert (after, an_after) in v_branch.access_nodes

def test_analyze_conditional_single_branch():
    """Non-exhaustive conditional (no else): fall-through keeps pre_ver."""
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)

    init = sdfg.add_state("init")
    an_init = init.add_write("s")
    init.add_edge(init.add_tasklet("t0", {}, {"o"}, "o=0"), "o", an_init, None, dace.Memlet("s[0]"))

    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())

    # Only one branch (no else) — writes s
    b1 = dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg)
    s1 = b1.add_state("s1")
    an1 = s1.add_write("s")
    s1.add_edge(s1.add_tasklet("t1", {}, {"o"}, "o=1"), "o", an1, None, dace.Memlet("s[0]"))
    cb.add_branch("s > 0", b1)

    after = sdfg.add_state("after")
    sdfg.add_edge(cb, after, dace.InterstateEdge())
    an_after = after.add_read("s")

    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)

    # Not exhaustive → fall-through keeps pre_ver → must merge branch write to pre_ver
    assert len(plan.versions) == 1
    v0 = plan.versions[0]
    assert (init, an_init) in v0.access_nodes
    assert (s1, an1) in v0.access_nodes
    assert (after, an_after) in v0.access_nodes


def test_analyze_conditional_partial_write():
    """When only one branch writes, merge back to pre-conditional version."""
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)

    init = sdfg.add_state("init")
    an_init = init.add_write("s")
    init.add_edge(init.add_tasklet("t0", {}, {"o"}, "o=0"), "o", an_init, None, dace.Memlet("s[0]"))

    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())

    # Branch 1: writes s
    b1 = dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg)
    s1 = b1.add_state("s1")
    an1 = s1.add_write("s")
    s1.add_edge(s1.add_tasklet("t1", {}, {"o"}, "o=1"), "o", an1, None, dace.Memlet("s[0]"))
    cb.add_branch("True", b1)

    # Branch 2: does NOT write s (just an empty state)
    b2 = dace.sdfg.state.ControlFlowRegion("b2", sdfg=sdfg)
    b2.add_state("s2")
    cb.add_branch(None, b2)

    after = sdfg.add_state("after")
    sdfg.add_edge(cb, after, dace.InterstateEdge())
    an_after = after.add_read("s")

    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)

    # One branch kept pre_ver, one wrote → pre_ver in versions → merge to pre_ver
    assert len(plan.versions) == 1
    v0 = plan.versions[0]
    assert (init, an_init) in v0.access_nodes
    assert (s1, an1) in v0.access_nodes
    assert (after, an_after) in v0.access_nodes


def test_analyze_map_scope():
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o=1")
    an1 = s1.add_write("s")
    s1.add_edge(t1, "o", an1, None, dace.Memlet("s[0]"))
    
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    ani = s2.add_read("s")
    me, mx = s2.add_map("m", dict(i="0:10"))
    t2 = s2.add_tasklet("t2", {"i"}, {"o"}, "o=i")
    ano = s2.add_write("s")
    
    s2.add_memlet_path(ani, me, t2, dst_conn="i", memlet=dace.Memlet("s[0]"))
    s2.add_memlet_path(t2, mx, ano, src_conn="o", memlet=dace.Memlet("s[0]"))
    
    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)
    
    # s1: an1 (write) -> v0
    # s2: ani (read) -> v0
    #     me (user)  -> v0
    #     t2 (user)  -> v0 (reaching from s1)
    #     mx (user)  -> v0
    #     ano (write)-> v1
    
    assert len(plan.versions) == 2
    v0, v1 = plan.versions
    assert (s1, an1) in v0.access_nodes
    assert (s2, ani) in v0.access_nodes
    assert me in v0.symbol_usages
    assert t2 in v0.symbol_usages
    assert mx in v0.symbol_usages
    
    assert (s2, ano) in v1.access_nodes

def test_analyze_nested_sdfg_symbol():
    sdfg = dace.SDFG("parent")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s1.add_edge(s1.add_tasklet("t1", {}, {"o"}, "o=1"), "o", s1.add_write("s"), None, dace.Memlet("s[0]"))
    
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    nsdfg = dace.SDFG("child")
    nsdfg.add_symbol("sym", dace.float64)
    nnode = s2.add_nested_sdfg(nsdfg, set(), set(), symbol_mapping={"sym": "s"})
    
    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)
    
    # nnode uses 's' in its symbol mapping
    assert len(plan.versions) == 1
    v0 = plan.versions[0]
    assert nnode in v0.symbol_usages

def test_analyze_double_write_conditional():
    sdfg = dace.SDFG("test")
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
    b1 = dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg)
    cb.add_branch("x > 0", b1)
    
    vmap = SSAVersionMap()
    targets = {"x"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)
    
    # an1 (write) -> v0
    # an_r (read)  -> v0
    # an2 (write) -> v1
    # cb (usage)   -> v1
    assert len(plan.versions) == 2
    v0, v1 = plan.versions
    assert (s1, an1) in v0.access_nodes
    assert (s2, an_r) in v0.access_nodes
    assert (s2, an2) in v1.access_nodes
    assert cb in v1.symbol_usages

def test_analyze_loop_carried_via_conditional_metadata():
    """Carry detection must see reads in ConditionalBlock branch conditions."""
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)

    # First block in loop body: ConditionalBlock reading s in its condition
    cb = dace.sdfg.state.ConditionalBlock("cb")
    loop.add_node(cb, is_start_block=True)
    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("s > 0", branch)

    # Second block: write s
    ls = loop.add_state("ls")
    loop.add_edge(cb, ls, dace.InterstateEdge())
    an_w = ls.add_write("s")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", an_w, None, dace.Memlet("s[0]"))

    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)

    # ConditionalBlock reads s before write → loop-carried → revert
    assert len(plan.versions) == 0


def test_analyze_loop_carried_via_nested_loop_metadata():
    """Carry detection must see reads in nested LoopRegion conditions."""
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)

    outer = dace.sdfg.state.LoopRegion("outer", "j < 10", "j", "j=0", "j=j+1")
    sdfg.add_node(outer)

    # First block: nested loop with condition referencing s
    inner = dace.sdfg.state.LoopRegion("inner", "i < s", "i", "i=0", "i=i+1")
    outer.add_node(inner, is_start_block=True)
    inner.add_state("dummy")

    # Second block: write s
    ls = outer.add_state("ls")
    outer.add_edge(inner, ls, dace.InterstateEdge())
    an_w = ls.add_write("s")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", an_w, None, dace.Memlet("s[0]"))

    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)

    # Nested loop condition reads s before write → loop-carried → revert
    assert len(plan.versions) == 0


def test_analyze_loop_carried_via_interstate_edge():
    """Carry detection must see reads in interstate edge conditions."""
    sdfg = dace.SDFG("test")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)

    # First state: empty guard
    guard = loop.add_state("guard")
    # Second state: write s
    ls = loop.add_state("ls")
    # Interstate edge from guard to ls references s
    loop.add_edge(guard, ls, dace.InterstateEdge(condition="s > 0"))
    an_w = ls.add_write("s")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", an_w, None, dace.Memlet("s[0]"))

    vmap = SSAVersionMap()
    targets = {"s"}
    plan = SSAPlan()
    analyze_cfg(sdfg, vmap, targets, plan)

    # Interstate edge reads s before write → loop-carried → revert
    assert len(plan.versions) == 0


if __name__ == "__main__":
    test_analyze_sequential()
    test_analyze_loop_carried()
    test_analyze_loop_carried_multi_state()
    test_analyze_loop_carried_multi_state_with_pre_def()
    test_analyze_loop_carried_write_node_first()
    test_analyze_loop_carried_with_pre_def()
    test_analyze_conditional_mismatch()
    test_analyze_conditional_single_branch()
    test_analyze_conditional_partial_write()
    test_analyze_map_scope()
    test_analyze_nested_sdfg_symbol()
    test_analyze_double_write_conditional()
    test_analyze_loop_carried_via_conditional_metadata()
    test_analyze_loop_carried_via_nested_loop_metadata()
    test_analyze_loop_carried_via_interstate_edge()
    print("All SSA analysis tests passed!")
