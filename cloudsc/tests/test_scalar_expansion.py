
import dace
import pytest
import sympy as sp
from ssa.scalar_expansion import (
    find_expansion_targets, apply_scalar_expansion,
    _build_expansion_plan, _analyze_external_accesses, _find_enclosing_target_loop,
    ExpansionStrategy
)

def test_find_expansion_targets_basic():
    sdfg = dace.SDFG("test_find")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # Loop 0 to 9
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    ls.add_tasklet("t", {}, {"out"}, "out = 1.0")
    ls.add_edge(ls.nodes()[-1], "out", ls.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Add an access node OUTSIDE the loop to make it truly "blocked" from LoopToMap perspective
    after_state = sdfg.add_state("after")
    sdfg.add_edge(loop, after_state, dace.InterstateEdge())
    after_state.add_read("s")
    
    targets = find_expansion_targets(sdfg)
    assert "s" in targets
    assert len(targets["s"]) == 1
    assert int(targets["s"][0]["dim_size"]) == 10

def test_find_expansion_targets_force():
    sdfg = dace.SDFG("test_force")
    sdfg.add_scalar("s", dace.float64, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    # Correct write (already indexed by i) -> normally not blocked
    ls.add_tasklet("t", {}, {"out"}, "out = 1.0")
    ls.add_edge(ls.nodes()[-1], "out", ls.add_write("s"), None, dace.Memlet("s[i]"))
    
    # Without force, should be empty
    assert "s" not in find_expansion_targets(sdfg)
    
    # With force, should find it
    targets = find_expansion_targets(sdfg, force={"s"})
    assert "s" in targets

def test_apply_scalar_expansion_basic():
    sdfg = dace.SDFG("test_apply")
    sdfg.add_scalar("s", dace.float64, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    t = ls.add_tasklet("t", {"in1"}, {"out1"}, "out1 = in1")
    ls.add_edge(ls.add_read("s"), None, t, "in1", dace.Memlet("s[0]"))
    ls.add_edge(t, "out1", ls.add_write("s"), None, dace.Memlet("s[0]"))
    
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Check new array
    ext_name = "s_ext"
    assert ext_name in sdfg.arrays
    assert list(sdfg.arrays[ext_name].shape) == [10]
    
    # Check memlets
    for edge in ls.edges():
        assert edge.data.data == ext_name
        # Index should be i - 0 = i
        assert str(edge.data.subset) == "i"

def test_apply_scalar_expansion_nested_sdfg():
    sdfg = dace.SDFG("parent")
    sdfg.add_scalar("s", dace.float64, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    
    nsdfg = dace.SDFG("child")
    nsdfg.add_scalar("s_internal_array", dace.float64) # internal scalar array
    # Map external 's' to internal symbol 's_sym'
    nnode = ls.add_nested_sdfg(nsdfg, {}, {"s_internal_array"}, {"s_sym": dace.symbol("s")})
    ls.add_edge(nnode, "s_internal_array", ls.add_write("s"), None, dace.Memlet("s[0]"))
    
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # The symbol mapping should now point to the expanded array with index
    val_str = str(nnode.symbol_mapping["s_sym"])
    assert "s_ext" in val_str and "i" in val_str

def test_apply_scalar_expansion_epilogue():
    sdfg = dace.SDFG("epilogue")
    sdfg.add_scalar("s", dace.float64, transient=True)
    # Loop 0 to 9
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    ls.add_tasklet("t1", {}, {"out"}, "out = 1.0")
    ls.add_edge(ls.nodes()[-1], "out", ls.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Access AFTER loop
    after_state = sdfg.add_state("after")
    sdfg.add_edge(loop, after_state, dace.InterstateEdge())
    after_state.add_tasklet("t2", {"in1"}, {}, "x = in1")
    after_state.add_edge(after_state.add_read("s"), None, after_state.nodes()[-2], "in1", dace.Memlet("s[0]"))
    
    # This should trigger Epilogue (all loops write first, external access is after)
    targets = find_expansion_targets(sdfg)
    assert "s" in targets
    apply_scalar_expansion(sdfg, "s", targets["s"])
    
    # External access in 'after' should be redirected to s_ext[9] (last element)
    for edge in after_state.edges():
        if edge.data.data == "s_ext":
            assert str(edge.data.subset) == "9"
    # Original scalar should be removed
    assert "s" not in sdfg.arrays

def test_apply_scalar_expansion_prologue():
    sdfg = dace.SDFG("prologue")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # Access BEFORE loop
    before_state = sdfg.add_state("before", is_start_block=True)
    t1 = before_state.add_tasklet("t1", {}, {"out"}, "out = 1.0")
    before_state.add_edge(t1, "out", before_state.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Loop 0 to 9
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop)
    sdfg.add_edge(before_state, loop, dace.InterstateEdge())
    ls = loop.add_state("loop_state")
    # Loop WRITES to 's' (Pure write to trigger original 'Type A' logic)
    t2 = ls.add_tasklet("t2", {}, {"out1"}, "out1 = 2.0")
    ls.add_edge(t2, "out1", ls.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Add access AFTER to make it blocked
    after = sdfg.add_state("after")
    sdfg.add_edge(loop, after, dace.InterstateEdge())
    after.add_read("s")

    # REDIRECT strategy: all accesses (before, inside, after) are rewritten to s_ext[idx]
    targets = find_expansion_targets(sdfg)
    assert "s" in targets
    apply_scalar_expansion(sdfg, "s", targets["s"])

    # Pre-loop write should now target s_ext[0] (redirected)
    before_nodes = [n for n in before_state.nodes() if isinstance(n, dace.nodes.AccessNode)]
    assert any("s_ext" in n.data for n in before_nodes)
    # Original scalar should be removed
    assert "s" not in sdfg.arrays

def test_apply_scalar_expansion_different_parents():
    sdfg = dace.SDFG("diff_parents")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # Branch 1
    b1 = dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg)
    sdfg.add_node(b1, is_start_block=True)
    loop1 = dace.sdfg.state.LoopRegion("loop1", "i < 10", "i", "i = 0", "i = i + 1")
    b1.add_node(loop1, is_start_block=True)
    ls1 = loop1.add_state("ls1")
    t1 = ls1.add_tasklet("t1", {}, {"o"}, "o = 1")
    ls1.add_edge(t1, "o", ls1.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Branch 2
    b2 = dace.sdfg.state.ControlFlowRegion("b2", sdfg=sdfg)
    sdfg.add_node(b2)
    loop2 = dace.sdfg.state.LoopRegion("loop2", "j < 10", "j", "j = 0", "j = j + 1")
    b2.add_node(loop2, is_start_block=True)
    ls2 = loop2.add_state("ls2")
    t2 = ls2.add_tasklet("t2", {}, {"o"}, "o = 1")
    ls2.add_edge(t2, "o", ls2.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Add access after both branches to make them "blocked"
    after = sdfg.add_state("after")
    sdfg.add_edge(b1, after, dace.InterstateEdge())
    sdfg.add_edge(b2, after, dace.InterstateEdge())
    after.add_read("s")

    # Without the fix, these might be merged and cause ValueError because loop1 is not in b2
    targets = find_expansion_targets(sdfg, force={"s"})
    # Should have 2 separate targets because parents are different
    assert len(targets["s"]) == 2
    
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    assert "s_ext" in sdfg.arrays

def test_apply_scalar_expansion_siblings():
    sdfg = dace.SDFG("siblings")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # Loop 1: 0 to 9
    loop1 = dace.sdfg.state.LoopRegion("loop1", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop1, is_start_block=True)
    ls1 = loop1.add_state("ls1")
    ls1.add_tasklet("t1", {}, {"out"}, "out = 1.0")
    ls1.add_edge(ls1.nodes()[-1], "out", ls1.add_write("s"), None, dace.Memlet("s[0]"))
    
    # Loop 2: 0 to 9 (sibling)
    loop2 = dace.sdfg.state.LoopRegion("loop2", "j < 10", "j", "j = 0", "j = j + 1")
    sdfg.add_node(loop2)
    sdfg.add_edge(loop1, loop2, dace.InterstateEdge())
    ls2 = loop2.add_state("ls2")
    ls2.add_tasklet("t2", {"in1"}, {}, "x = in1")
    ls2.add_edge(ls2.add_read("s"), None, ls2.nodes()[-2], "in1", dace.Memlet("s[0]"))
    
    # Add loose access to block it
    after = sdfg.add_state("after")
    sdfg.add_edge(loop2, after, dace.InterstateEdge())
    after.add_read("s")
    
    targets = find_expansion_targets(sdfg)
    assert len(targets["s"]) == 1  # Both loops merged into one expansion target
    assert loop1 in targets["s"][0]["loops"]
    assert loop2 in targets["s"][0]["loops"]
    apply_scalar_expansion(sdfg, "s", targets["s"])
    
    # Both loops should use same s_ext, but different itervars
    assert str(ls1.edges()[0].data.subset) == "i"
    assert str(ls2.edges()[0].data.subset) == "j"

def test_apply_scalar_expansion_tasklet():
    sdfg = dace.SDFG("tasklet")
    sdfg.add_scalar("s", dace.float64, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    # Tasklet using 's' as a symbol/literal name in code (though usually it should be through connectors)
    t = ls.add_tasklet("t", {}, {}, "s = 1.0")
    
    # Force expansion
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Tasklet code should be updated
    assert "s_ext[i]" in t.code.as_string or "s_ext(i)" in t.code.as_string

def test_apply_scalar_expansion_symbolic_heuristic():
    sdfg = dace.SDFG("heuristic")
    sdfg.add_symbol("klon", dace.int32)
    sdfg.add_scalar("s", dace.float64, transient=True)
    # Loop with jl variable
    loop = dace.sdfg.state.LoopRegion("myloop", "jl < klon", "jl", "jl = 0", "jl = 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    ls.add_tasklet("t", {}, {"out"}, "out = 1.0")
    ls.add_edge(ls.nodes()[-1], "out", ls.add_write("s"), None, dace.Memlet("s[0]"))
    
    targets = find_expansion_targets(sdfg, force={"s"})
    # Heuristic should kick in for 'jl' -> klon
    assert str(targets["s"][0]["dim_size"]) == "klon"
    
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    assert str(sdfg.arrays["s_ext"].shape[0]) == "klon"

def test_apply_scalar_expansion_conditional():
    sdfg = dace.SDFG("conditional")
    sdfg.add_scalar("s", dace.float64, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    
    # ConditionalBlock inside loop using 's' in branch condition
    cb = dace.sdfg.state.ConditionalBlock("mycond")
    ls.parent_graph.add_node(cb)
    ls.parent_graph.add_edge(ls, cb, dace.InterstateEdge())
    
    branch_body = dace.sdfg.state.ControlFlowRegion("branch", sdfg=sdfg)
    branch_state = branch_body.add_state("branch_state")
    cb.add_branch("s > 0", branch_body)
    
    # Force expansion
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Condition should be updated
    cond_str = cb.branches[0][0].as_string
    assert "s_ext[i]" in cond_str or "s_ext(i)" in cond_str

def test_apply_scalar_expansion_loop_metadata():
    sdfg = dace.SDFG("loop_metadata")
    # Scalar to be expanded
    sdfg.add_scalar("s_scalar", dace.float64, transient=True)
    
    # Outer loop with constant bounds
    outer = dace.sdfg.state.LoopRegion("outer", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(outer, is_start_block=True)
    
    # Inner loop using 's_scalar' in its initialization
    inner = dace.sdfg.state.LoopRegion("inner", "j < 10", "j", "j = s_scalar", "j = j + 1")
    outer.add_node(inner, is_start_block=True)
    ls = inner.add_state("ls")
    
    # Force expansion of 's_scalar'
    targets = find_expansion_targets(sdfg, force={"s_scalar"})
    apply_scalar_expansion(sdfg, "s_scalar", targets["s_scalar"], force={"s_scalar"})
    
    # Inner loop init should be updated to s_scalar_ext[i]
    init_str = inner.init_statement.as_string
    assert "s_scalar_ext" in init_str and "i" in init_str

def test_apply_scalar_expansion_nested_validation():
    sdfg = dace.SDFG("parent")
    sdfg.add_scalar("s", dace.float64, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    
    # Internal SDFG that expects 's' as a SYMBOL
    nsdfg = dace.SDFG("child")
    nsdfg.add_symbol("s_sym", dace.float64)
    cs = nsdfg.add_state("cs")
    cs.add_tasklet("ct", {}, {}, "x = s_sym")
    
    nnode = ls.add_nested_sdfg(nsdfg, {}, set(), {"s_sym": dace.symbol("s")})
    
    # Force expansion of 's'
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Validation check - this is where it was failing
    sdfg.validate()
    
    # The mapping should now use the array with index
    val_str = str(nnode.symbol_mapping["s_sym"])
    assert "s_ext" in val_str and "i" in val_str

def test_apply_scalar_expansion_interstate_edge():
    sdfg = dace.SDFG("interstate_edge")
    sdfg.add_scalar("s", dace.float64, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    
    s1 = loop.add_state("s1")
    s2 = loop.add_state("s2")
    edge = loop.add_edge(s1, s2, dace.InterstateEdge(condition="s > 0"))
    
    # Force expansion
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Edge condition should be updated
    cond_str = edge.data.condition.as_string
    assert "s_ext[i]" in cond_str or "s_ext(i)" in cond_str

def test_apply_scalar_expansion_conditional_direct():
    sdfg = dace.SDFG("cond_direct")
    sdfg.add_scalar("s", dace.bool_, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    
    # ConditionalBlock inside loop using 's' DIRECTLY as condition
    cb = dace.sdfg.state.ConditionalBlock("mycond")
    loop.add_node(cb, is_start_block=True)
    
    branch_body = dace.sdfg.state.ControlFlowRegion("branch", sdfg=sdfg)
    branch_body.add_state("branch_state")
    # Using 's' directly as condition string
    cb.add_branch("s", branch_body)
    
    # Force expansion
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Condition should be updated to s_ext[i]
    cond_str = cb.branches[0][0].as_string
    assert "s_ext[i]" in cond_str or "s_ext(i)" in cond_str

def test_apply_scalar_expansion_nested_conditional():
    sdfg = dace.SDFG("nested_cond")
    sdfg.add_scalar("s", dace.bool_, transient=True)
    outer = dace.sdfg.state.LoopRegion("outer", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(outer, is_start_block=True)
    
    # Nested region inside outer loop
    inner_region = dace.sdfg.state.ControlFlowRegion("inner_reg", sdfg=sdfg)
    outer.add_node(inner_region, is_start_block=True)
    
    # ConditionalBlock inside inner region
    cb = dace.sdfg.state.ConditionalBlock("cb")
    inner_region.add_node(cb, is_start_block=True)
    
    branch = dace.sdfg.state.ControlFlowRegion("branch", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("s", branch)
    
    # Force expansion
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Condition should be updated to s_ext[i]
    cond_str = cb.branches[0][0].as_string
    assert "s_ext[i]" in cond_str or "s_ext(i)" in cond_str

def test_apply_scalar_expansion_multi_branch_conditional():
    sdfg = dace.SDFG("multi_branch")
    sdfg.add_scalar("s", dace.float64, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    
    cb = dace.sdfg.state.ConditionalBlock("mycond")
    loop.add_node(cb, is_start_block=True)
    
    # Branch 1
    b1 = dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg)
    b1.add_state("s1")
    cb.add_branch("s > 0", b1)
    
    # Branch 2
    b2 = dace.sdfg.state.ControlFlowRegion("b2", sdfg=sdfg)
    b2.add_state("s2")
    cb.add_branch("s < 0", b2)
    
    # Force expansion
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # BOTH branch conditions should be updated
    assert "s_ext[i] > 0" in cb.branches[0][0].as_string
    assert "s_ext[i] < 0" in cb.branches[1][0].as_string

def test_apply_scalar_expansion_nested_sdfg_conditional():
    sdfg = dace.SDFG("parent")
    sdfg.add_scalar("s", dace.bool_, transient=True)
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    
    # Internal SDFG with a ConditionalBlock using 's'
    nsdfg = dace.SDFG("child")
    nsdfg.add_scalar("s", dace.bool_, transient=False)
    cs = nsdfg.add_state("cs")
    cb = dace.sdfg.state.ConditionalBlock("cb")
    nsdfg.add_node(cb)
    nsdfg.add_edge(cs, cb, dace.InterstateEdge())
    
    branch = dace.sdfg.state.ControlFlowRegion("branch", sdfg=nsdfg)
    branch.add_state("bs")
    cb.add_branch("s", branch)
    
    # Nested SDFG node in parent mapping 's' to 's'
    nnode = ls.add_nested_sdfg(nsdfg, {"s"}, set(), {})
    an = ls.add_access("s")
    ls.add_edge(an, None, nnode, "s", dace.Memlet("s[0]"))
    
    # Force expansion of 's'
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Condition inside NestedSDFG should be renamed but NOT indexed
    # because it is now a scalar connector.
    cond_str = cb.branches[0][0].as_string
    assert "s_ext" in cond_str and "[" not in cond_str

def test_apply_scalar_expansion_epilogue_metadata():
    sdfg = dace.SDFG("epilogue_metadata")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # 1. Loop that writes s
    loop = dace.sdfg.state.LoopRegion("myloop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("ls")
    t = ls.add_tasklet("t", {}, {"o"}, "o = 1")
    ls.add_edge(t, "o", ls.add_write("s"), None, dace.Memlet("s[0]"))
    
    # 2. Access after loop (Epilogue)
    after = sdfg.add_state("after")
    sdfg.add_edge(loop, after, dace.InterstateEdge())
    t2 = after.add_tasklet("t2", {"in1"}, {}, "x = in1")
    after.add_edge(after.add_read("s"), None, t2, "in1", dace.Memlet("s[0]"))
    
    # 3. Conditional Block after loop
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(after, cb, dace.InterstateEdge())
    branch = dace.sdfg.state.ControlFlowRegion("branch", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("s > 0", branch)
    
    # Force expansion
    targets = find_expansion_targets(sdfg, force={"s"})
    apply_scalar_expansion(sdfg, "s", targets["s"], force={"s"})
    
    # Condition in CB should be updated to s_ext[9] (or end index)
    cond_str = cb.branches[0][0].as_string
    assert "s_ext[9]" in cond_str or "s_ext(9)" in cond_str

# ---------------------------------------------------------------------------
# Phase 1 (analysis) tests
# ---------------------------------------------------------------------------

def test_analyze_external_skip():
    """Access between two sibling loops should return SKIP."""
    sdfg = dace.SDFG("test_skip")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop1 = dace.sdfg.state.LoopRegion("l1", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop1, is_start_block=True)
    ls1 = loop1.add_state("ls1")
    ls1.add_edge(ls1.add_tasklet("t", {}, {"o"}, "o=1"), "o", ls1.add_write("s"), None, dace.Memlet("s[0]"))

    mid = sdfg.add_state("mid")
    sdfg.add_edge(loop1, mid, dace.InterstateEdge())
    mid.add_read("s")  # access between loops

    loop2 = dace.sdfg.state.LoopRegion("l2", "j < 10", "j", "j=0", "j=j+1")
    sdfg.add_node(loop2)
    sdfg.add_edge(mid, loop2, dace.InterstateEdge())
    ls2 = loop2.add_state("ls2")
    ls2.add_edge(ls2.add_tasklet("t2", {}, {"o"}, "o=2"), "o", ls2.add_write("s"), None, dace.Memlet("s[0]"))

    strategy = _analyze_external_accesses(sdfg, "s", {loop1, loop2}, sdfg)
    assert strategy == ExpansionStrategy.SKIP


def test_analyze_external_redirect():
    """Access before or after loops should return REDIRECT."""
    sdfg = dace.SDFG("test_redirect")
    sdfg.add_scalar("s", dace.float64, transient=True)

    before = sdfg.add_state("before", is_start_block=True)
    before.add_edge(before.add_tasklet("t", {}, {"o"}, "o=1"), "o", before.add_write("s"), None, dace.Memlet("s[0]"))

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    sdfg.add_edge(before, loop, dace.InterstateEdge())
    ls = loop.add_state("ls")
    ls.add_edge(ls.add_tasklet("t2", {}, {"o"}, "o=2"), "o", ls.add_write("s"), None, dace.Memlet("s[0]"))

    strategy = _analyze_external_accesses(sdfg, "s", {loop}, sdfg)
    assert strategy == ExpansionStrategy.REDIRECT


def test_analyze_external_internal_only():
    """No access outside loop should return INTERNAL_ONLY."""
    sdfg = dace.SDFG("test_internal")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("ls")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", ls.add_write("s"), None, dace.Memlet("s[0]"))

    strategy = _analyze_external_accesses(sdfg, "s", {loop}, sdfg)
    assert strategy == ExpansionStrategy.INTERNAL_ONLY


def test_find_enclosing_target_loop():
    """Should find the nearest target loop, skipping non-target loops."""
    sdfg = dace.SDFG("test_enclosing")
    sdfg.add_scalar("s", dace.float64, transient=True)

    outer = dace.sdfg.state.LoopRegion("outer", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(outer, is_start_block=True)
    inner = dace.sdfg.state.LoopRegion("inner", "j < 5", "j", "j=0", "j=j+1")
    outer.add_node(inner, is_start_block=True)
    ls = inner.add_state("ls")

    # Only outer is a target loop
    assert _find_enclosing_target_loop(ls, {outer}) == outer
    # Only inner is a target loop
    assert _find_enclosing_target_loop(ls, {inner}) == inner
    # Neither is a target
    assert _find_enclosing_target_loop(ls, set()) is None


def test_build_plan_access_nodes():
    """Plan should contain access_node entries for all AccessNodes referencing the scalar."""
    sdfg = dace.SDFG("test_plan_an")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("ls")
    t = ls.add_tasklet("t", {"inp"}, {"out"}, "out = inp")
    an_r = ls.add_read("s")
    an_w = ls.add_write("s")
    ls.add_edge(an_r, None, t, "inp", dace.Memlet("s[0]"))
    ls.add_edge(t, "out", an_w, None, dace.Memlet("s[0]"))

    info = {"loops": {loop}, "parent": sdfg, "offset": "0", "start": "0", "end": "9"}
    plan = _build_expansion_plan(sdfg, "s", info)

    an_sites = [(s, n) for typ, s, n, _ in plan if typ == "access_node"]
    assert len(an_sites) == 2
    assert (ls, an_r) in an_sites
    assert (ls, an_w) in an_sites


def test_build_plan_metadata():
    """Plan should include metadata entries for LoopRegion and ConditionalBlock refs."""
    sdfg = dace.SDFG("test_plan_meta")
    sdfg.add_scalar("s", dace.float64, transient=True)

    outer = dace.sdfg.state.LoopRegion("outer", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(outer, is_start_block=True)

    inner = dace.sdfg.state.LoopRegion("inner", "j < s", "j", "j=0", "j=j+1")
    outer.add_node(inner, is_start_block=True)
    inner.add_state("ls")

    info = {"loops": {outer}, "parent": sdfg, "offset": "0", "start": "0", "end": "9"}
    plan = _build_expansion_plan(sdfg, "s", info)

    meta_blocks = [b for typ, b, _, _ in plan if typ == "metadata"]
    assert inner in meta_blocks


def test_build_plan_interstate_edge():
    """Plan should include edge entries for interstate edges referencing the scalar."""
    sdfg = dace.SDFG("test_plan_edge")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)

    s1 = loop.add_state("s1")
    s2 = loop.add_state("s2")
    edge = loop.add_edge(s1, s2, dace.InterstateEdge(condition="s > 0"))

    info = {"loops": {loop}, "parent": sdfg, "offset": "0", "start": "0", "end": "9"}
    plan = _build_expansion_plan(sdfg, "s", info)

    edge_sites = [e for typ, e, _, _ in plan if typ == "edge"]
    assert edge.data in edge_sites


def test_build_plan_tasklet_code():
    """Plan should detect scalar name in tasklet code even without connectors."""
    sdfg = dace.SDFG("test_plan_tasklet")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("ls")
    t = ls.add_tasklet("t", {}, {}, "s = 1.0")

    info = {"loops": {loop}, "parent": sdfg, "offset": "0", "start": "0", "end": "9"}
    plan = _build_expansion_plan(sdfg, "s", info)

    nc_sites = [(s, n) for typ, s, n, _ in plan if typ == "node_content"]
    assert (ls, t) in nc_sites


def test_build_plan_nested_cond_in_loop():
    """Plan should find refs inside a ConditionalBlock nested in a target loop."""
    sdfg = dace.SDFG("test_plan_nested_cond")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)

    cb = dace.sdfg.state.ConditionalBlock("cb")
    loop.add_node(cb, is_start_block=True)
    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    bs = branch.add_state("bs")
    bs.add_edge(bs.add_tasklet("t", {}, {"o"}, "o=1"), "o", bs.add_write("s"), None, dace.Memlet("s[0]"))
    cb.add_branch("s > 0", branch)

    info = {"loops": {loop}, "parent": sdfg, "offset": "0", "start": "0", "end": "9"}
    plan = _build_expansion_plan(sdfg, "s", info)

    # Should find: access_node in bs, metadata on cb, and the condition
    an_sites = [(s, n) for typ, s, n, _ in plan if typ == "access_node"]
    meta_sites = [b for typ, b, _, _ in plan if typ == "metadata"]
    assert any(s == bs for s, _ in an_sites)
    assert cb in meta_sites


def test_build_plan_idx_before_loop():
    """Sites before the loop in topo order should get index 0."""
    sdfg = dace.SDFG("test_plan_before")
    sdfg.add_scalar("s", dace.float64, transient=True)

    before = sdfg.add_state("before", is_start_block=True)
    before.add_edge(before.add_tasklet("t", {}, {"o"}, "o=1"), "o", before.add_write("s"), None, dace.Memlet("s[0]"))

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    sdfg.add_edge(before, loop, dace.InterstateEdge())
    ls = loop.add_state("ls")
    ls.add_edge(ls.add_tasklet("t2", {}, {"o"}, "o=2"), "o", ls.add_write("s"), None, dace.Memlet("s[0]"))

    info = {"loops": {loop}, "parent": sdfg, "offset": "0", "start": "0", "end": "9"}
    plan = _build_expansion_plan(sdfg, "s", info)

    # The access_node in 'before' state should have idx 0
    before_sites = [(s, n, idx) for typ, s, n, idx in plan if typ == "access_node" and s == before]
    assert len(before_sites) == 1
    assert before_sites[0][2] == sp.Integer(0)

    # The access_node in loop state should have idx i
    loop_sites = [(s, n, idx) for typ, s, n, idx in plan if typ == "access_node" and s == ls]
    assert len(loop_sites) == 1
    assert str(loop_sites[0][2]) == "i"


def test_empty_plan_does_not_remove_descriptor():
    """If _build_expansion_plan returns empty, the scalar descriptor must survive."""
    sdfg = dace.SDFG("test_empty_plan")
    # Scalar declared but never referenced in any state
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)
    loop.add_state("ls")  # empty state, no 's' usage

    fake_target = [{
        "start": "0", "end": "9", "dim_size": 10,
        "offset": "0", "loops": {loop}, "parent": sdfg,
    }]
    result = apply_scalar_expansion(sdfg, "s", fake_target)
    assert result == 0
    # Descriptor must still exist
    assert "s" in sdfg.arrays
    # No ext array should have been created
    assert "s_ext" not in sdfg.arrays


def test_skipped_target_preserves_descriptor():
    """If the target is SKIP (inter-loop access), descriptor must survive."""
    sdfg = dace.SDFG("test_skip_preserve")
    sdfg.add_scalar("s", dace.float64, transient=True)

    # Two sibling loops with 's' accessed BETWEEN them → SKIP
    loop1 = dace.sdfg.state.LoopRegion("l1", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop1, is_start_block=True)
    ls1 = loop1.add_state("ls1")
    ls1.add_edge(ls1.add_tasklet("t1", {}, {"o"}, "o=1"), "o", ls1.add_write("s"), None, dace.Memlet("s[0]"))

    mid = sdfg.add_state("mid")
    sdfg.add_edge(loop1, mid, dace.InterstateEdge())
    mid.add_read("s")  # access between the two loops

    loop2 = dace.sdfg.state.LoopRegion("l2", "j < 10", "j", "j=0", "j=j+1")
    sdfg.add_node(loop2)
    sdfg.add_edge(mid, loop2, dace.InterstateEdge())
    ls2 = loop2.add_state("ls2")
    ls2.add_edge(ls2.add_tasklet("t2", {}, {"o"}, "o=2"), "o", ls2.add_write("s"), None, dace.Memlet("s[0]"))

    # Both loops in the same target → mid is inter-loop → SKIP
    targets = [{
        "start": "0", "end": "9", "dim_size": 10,
        "offset": "0", "loops": {loop1, loop2}, "parent": sdfg,
    }]
    result = apply_scalar_expansion(sdfg, "s", targets)
    assert result == 0
    # Descriptor must survive
    assert "s" in sdfg.arrays
    assert "s_ext" not in sdfg.arrays


def test_multi_target_exclude_loops():
    """Each target's plan must NOT rewrite references inside the other target's loops."""
    sdfg = dace.SDFG("test_multi_excl")
    sdfg.add_scalar("s", dace.float64, transient=True)

    # Two loops with different ranges, both writing 's'
    loop1 = dace.sdfg.state.LoopRegion("l1", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop1, is_start_block=True)
    ls1 = loop1.add_state("ls1")
    ls1.add_edge(ls1.add_tasklet("t1", {}, {"o"}, "o=1"), "o", ls1.add_write("s"), None, dace.Memlet("s[0]"))

    loop2 = dace.sdfg.state.LoopRegion("l2", "j < 5", "j", "j=0", "j=j+1")
    sdfg.add_node(loop2)
    sdfg.add_edge(loop1, loop2, dace.InterstateEdge())
    ls2 = loop2.add_state("ls2")
    ls2.add_edge(ls2.add_tasklet("t2", {}, {"o"}, "o=2"), "o", ls2.add_write("s"), None, dace.Memlet("s[0]"))

    targets = [
        {"start": "0", "end": "9", "dim_size": 10,
         "offset": "0", "loops": {loop1}, "parent": sdfg},
        {"start": "0", "end": "4", "dim_size": 5,
         "offset": "0", "loops": {loop2}, "parent": sdfg},
    ]

    # Build plan for target 1 — should NOT include refs from loop2
    plan1 = _build_expansion_plan(sdfg, "s", targets[0], exclude_loops={loop2})
    plan1_states = {s for typ, s, _, _ in plan1 if typ == "access_node"}
    assert ls1 in plan1_states
    assert ls2 not in plan1_states

    # Build plan for target 2 — should NOT include refs from loop1
    plan2 = _build_expansion_plan(sdfg, "s", targets[1], exclude_loops={loop1})
    plan2_states = {s for typ, s, _, _ in plan2 if typ == "access_node"}
    assert ls2 in plan2_states
    assert ls1 not in plan2_states


def test_remove_data_after_all_targets():
    """Scalar descriptor should be removed only when all targets succeed."""
    sdfg = dace.SDFG("test_remove_all")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("ls")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", ls.add_write("s"), None, dace.Memlet("s[0]"))

    targets = [{"start": "0", "end": "9", "dim_size": 10,
                "offset": "0", "loops": {loop}, "parent": sdfg}]
    result = apply_scalar_expansion(sdfg, "s", targets)
    assert result == 1
    # Single target, no skips → descriptor removed
    assert "s" not in sdfg.arrays
    assert "s_ext" in sdfg.arrays


if __name__ == "__main__":
    test_find_expansion_targets_basic()
    test_find_expansion_targets_force()
    test_apply_scalar_expansion_basic()
    test_apply_scalar_expansion_nested_sdfg()
    test_apply_scalar_expansion_epilogue()
    test_apply_scalar_expansion_prologue()
    test_apply_scalar_expansion_different_parents()
    test_apply_scalar_expansion_siblings()
    test_apply_scalar_expansion_tasklet()
    test_apply_scalar_expansion_symbolic_heuristic()
    test_apply_scalar_expansion_conditional()
    test_apply_scalar_expansion_loop_metadata()
    test_apply_scalar_expansion_nested_validation()
    test_apply_scalar_expansion_interstate_edge()
    test_apply_scalar_expansion_conditional_direct()
    test_apply_scalar_expansion_nested_conditional()
    test_apply_scalar_expansion_multi_branch_conditional()
    test_apply_scalar_expansion_nested_sdfg_conditional()
    test_apply_scalar_expansion_epilogue_metadata()
    test_analyze_external_skip()
    test_analyze_external_redirect()
    test_analyze_external_internal_only()
    test_find_enclosing_target_loop()
    test_build_plan_access_nodes()
    test_build_plan_metadata()
    test_build_plan_interstate_edge()
    test_build_plan_tasklet_code()
    test_build_plan_nested_cond_in_loop()
    test_build_plan_idx_before_loop()
    test_empty_plan_does_not_remove_descriptor()
    test_skipped_target_preserves_descriptor()
    test_multi_target_exclude_loops()
    test_remove_data_after_all_targets()
    print("All scalar expansion tests passed!")
