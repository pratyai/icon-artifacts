
import dace
import pytest
from ssa.unroll import propagate_constants

def test_propagate_constants_basic():
    sdfg = dace.SDFG("test_propagate")
    sdfg.add_symbol("N", dace.int32)
    sdfg.add_symbol("M", dace.int32)
    
    # Non-transient array: shape should be preserved
    sdfg.add_array("A", [dace.symbol("N")], dace.float64)
    # Transient array: shape should be replaced
    sdfg.add_array("B", [dace.symbol("N")], dace.float64, transient=True)
    
    state = sdfg.add_state()
    # Tasklet using symbols
    tasklet = state.add_tasklet("test", {"in1"}, {"out1"}, "out1 = in1 + M")
    state.add_edge(state.add_read("A"), None, tasklet, "in1", dace.Memlet("A[0]"))
    state.add_edge(tasklet, "out1", state.add_write("B"), None, dace.Memlet("B[0]"))
    
    # Propagate M=5, N=10
    symbol_map = {"M": 5, "N": 10}
    propagate_constants(sdfg, symbol_map)
    
    # Assertions
    assert "M" not in sdfg.symbols
    assert "N" in sdfg.symbols # Still used in A
    assert str(sdfg.arrays["A"].shape[0]) == "N"
    assert int(sdfg.arrays["B"].shape[0]) == 10
    sdfg.validate()

def test_propagate_constants_nested():
    sdfg = dace.SDFG("parent")
    sdfg.add_symbol("N", dace.int32)
    
    state = sdfg.add_state()
    nsdfg = dace.SDFG("child")
    nsdfg.add_symbol("N", dace.int32)
    nnode = state.add_nested_sdfg(nsdfg, {}, set(), {"N": dace.symbol("N")})
    
    propagate_constants(sdfg, {"N": 10})
    
    assert "N" not in sdfg.symbols
    assert str(nnode.symbol_mapping["N"]) == "10"
    sdfg.validate()

def test_propagate_constants_extensive():
    sdfg = dace.SDFG("extensive")
    sdfg.add_symbol("N", dace.int32)
    sdfg.add_symbol("M", dace.int32)
    
    state = sdfg.add_state("s1")
    state2 = sdfg.add_state("s2")
    sdfg.add_edge(state, state2, dace.InterstateEdge(condition="N > 5", assignments={"x": "M * 2"}))
    sdfg.add_array("A", [20], dace.float64)
    # Memlet using symbols
    tasklet = state.add_tasklet("t", {"in1"}, {"out1"}, "out1 = in1")
    state.add_edge(state.add_read("A"), None, tasklet, "in1", dace.Memlet("A[M:M+N]"))

    # ADD AN EDGE FOR OUT1 to avoid dangling connector error
    sdfg.add_array("B", [20], dace.float64, transient=True)
    state.add_edge(tasklet, "out1", state.add_write("B"), None, dace.Memlet("B[0]"))

    propagate_constants(sdfg, {"N": 10, "M": 2})

    
    edge = sdfg.edges()[0].data
    assert "10 > 5" in edge.condition.as_string
    assert "2 * 2" in str(edge.assignments["x"])
    
    memlet_edge = [e for e in state.edges() if e.data.data == "A"][0]
    assert str(memlet_edge.data.subset) == "2:12"
    sdfg.validate()

def test_propagate_constants_word_boundaries():
    sdfg = dace.SDFG("boundaries")
    sdfg.add_symbol("N", dace.int32)
    sdfg.add_symbol("NCLV", dace.int32)
    
    state = sdfg.add_state()
    tasklet = state.add_tasklet("t", {}, {}, "x = N + NCLV")
    
    propagate_constants(sdfg, {"N": 1})
    assert "NCLV" in sdfg.symbols
    assert "NCLV" in tasklet.free_symbols
    sdfg.validate()

def test_propagate_constants_deep_nesting():
    parent = dace.SDFG("parent")
    parent.add_symbol("N", dace.int32)
    
    child = dace.SDFG("child")
    child.add_symbol("N", dace.int32)
    
    grandchild = dace.SDFG("grandchild")
    grandchild.add_symbol("N", dace.int32)
    gs = grandchild.add_state()
    gs.add_tasklet("gt", {}, {}, "x = N")
    
    cs = child.add_state()
    gnode = cs.add_nested_sdfg(grandchild, {}, set(), {"N": dace.symbol("N")})
    
    ps = parent.add_state()
    cnode = ps.add_nested_sdfg(child, {}, set(), {"N": dace.symbol("N")})
    
    propagate_constants(parent, {"N": 42})
    
    assert "N" not in parent.symbols
    assert str(cnode.symbol_mapping["N"]) == "42"
    
    # DaCe's symbol_mapping values can be symbolic objects
    g_val = str(gnode.symbol_mapping["N"])
    assert g_val == "42" or g_val == "N" # N if it wasn't replaced, 42 if it was
    assert g_val == "42"
    parent.validate()

def test_propagate_constants_loops_and_conds():
    sdfg = dace.SDFG("loops_and_conds")
    sdfg.add_symbol("N", dace.int32)
    sdfg.add_symbol("M", dace.int32)
    
    # 1. LoopRegion
    # Signature: label, condition_expr, loop_var, initialize_expr, update_expr
    loop = dace.sdfg.state.LoopRegion(
        "myloop", 
        "i < N", 
        "i",
        "i = 0", 
        "i = i + 1"
    )
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    # Use M inside loop
    ls.add_tasklet("lt", {}, {}, f"x = M")
    
    # 2. ConditionalBlock
    cond = dace.sdfg.state.ConditionalBlock("mycond")
    sdfg.add_node(cond)
    sdfg.add_edge(loop, cond, dace.InterstateEdge())
    
    branch = dace.sdfg.state.ControlFlowRegion("branch_body", sdfg=sdfg)
    bs = branch.add_state("branch_state")
    bs.add_tasklet("bt", {}, {}, f"y = M + N")
    
    cond.add_branch("M > 0", branch)
    
    propagate_constants(sdfg, {"N": 10, "M": 5})
    
    # Assertions
    # Loop bounds/init/update
    assert "10" in loop.loop_condition.as_string
    # Tasklet inside loop
    assert "5" in [n.code.as_string for n in ls.nodes() if isinstance(n, dace.nodes.Tasklet)][0]
    
    # Conditional branch condition
    assert "5 > 0" in cond.branches[0][0].as_string
    # Tasklet inside branch
    assert "5 + 10" in [n.code.as_string for n in bs.nodes() if isinstance(n, dace.nodes.Tasklet)][0]
    
    sdfg.validate()

if __name__ == "__main__":
    test_propagate_constants_basic()
    test_propagate_constants_nested()
    test_propagate_constants_extensive()
    test_propagate_constants_word_boundaries()
    test_propagate_constants_deep_nesting()
    test_propagate_constants_loops_and_conds()
    print("All tests passed!")
