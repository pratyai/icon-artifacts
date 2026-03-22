
import copy
import numpy as np
import dace
import pytest
from ssa.unroll import unroll_loops, propagate_constants

def test_unroll_loops_basic():
    sdfg = dace.SDFG("test_unroll")
    sdfg.add_symbol("N", dace.int32)

    loop = dace.sdfg.state.LoopRegion(
        "myloop",
        "i < N",
        "i",
        "i = 0",
        "i = i + 1"
    )
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    ls.add_tasklet("lt", {}, {}, "x = i")

    propagate_constants(sdfg, {"N": 5})
    unroll_loops(sdfg)

    assert not any(isinstance(n, dace.sdfg.state.LoopRegion) for n in sdfg.nodes())
    states = list(sdfg.all_states())
    assert len(states) >= 5

    # Each unrolled copy gets a __ur suffix
    labels = [s.label for s in states]
    assert any("__ur" in l for l in labels)

    sdfg.validate()

def test_unroll_loops_no_match():
    """Loop with symbolic (unresolved) bounds should not be unrolled."""
    sdfg = dace.SDFG("test_no_unroll")
    sdfg.add_symbol("N", dace.int32)

    loop = dace.sdfg.state.LoopRegion(
        "myloop",
        "i < N",
        "i",
        "i = 0",
        "i = i + 1"
    )
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    ls.add_tasklet("lt", {}, {}, "x = i")

    # Don't propagate N — bounds stay symbolic
    unroll_loops(sdfg)

    assert any(isinstance(n, dace.sdfg.state.LoopRegion) for n in sdfg.nodes())

    sdfg.validate()

def test_unroll_loops_nested():
    sdfg = dace.SDFG("test_nested_unroll")
    sdfg.add_symbol("N", dace.int32)

    outer = dace.sdfg.state.LoopRegion("outer", "j < 2", "j", "j = 0", "j = j + 1")
    sdfg.add_node(outer, is_start_block=True)

    inner = dace.sdfg.state.LoopRegion("inner", "i < N", "i", "i = 0", "i = i + 1")
    outer.add_node(inner, is_start_block=True)
    is_state = inner.add_state("inner_state")
    is_state.add_tasklet("it", {}, {}, "x = i + j")

    propagate_constants(sdfg, {"N": 3})
    unroll_loops(sdfg)

    # Both loops have concrete counts <= 10, so both get unrolled
    assert not any(isinstance(n, dace.sdfg.state.LoopRegion) for n in sdfg.all_nodes_recursive())

    # 2 outer * 3 inner = 6 body states
    inner_states = [s for s in sdfg.all_states() if "inner_state" in s.label]
    assert len(inner_states) == 6

    sdfg.validate()

def test_unroll_loops_complex_range():
    # Loop from 10 down to 2, step -2 (10, 8, 6, 4, 2) -> 5 iterations
    sdfg = dace.SDFG("downward")
    sdfg.add_symbol("START", dace.int32)
    loop = dace.sdfg.state.LoopRegion(
        "downloop",
        "i >= 2",
        "i",
        "i = START",
        "i = i - 2"
    )
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("body")
    ls.add_tasklet("lt", {}, {}, "x = i")

    propagate_constants(sdfg, {"START": 10})
    unroll_loops(sdfg)

    states = list(sdfg.all_states())
    body_states = [s for s in states if "body" in s.label]
    assert len(body_states) == 5

    codes = sorted([s.nodes()[0].code.as_string for s in body_states])
    assert codes == ["x = 10", "x = 2", "x = 4", "x = 6", "x = 8"]

    sdfg.validate()

def test_unroll_loops_multi_round():
    sdfg = dace.SDFG("multiround")
    sdfg.add_symbol("N", dace.int32)
    sdfg.add_symbol("M", dace.int32)

    outer = dace.sdfg.state.LoopRegion("outer", "j < N", "j", "j = 0", "j = j + 1")
    sdfg.add_node(outer, is_start_block=True)

    inner = dace.sdfg.state.LoopRegion("inner", "i < M", "i", "i = 0", "i = i + 1")
    outer.add_node(inner, is_start_block=True)
    inner.add_state("body").add_tasklet("t", {}, {}, "x = i + j")

    propagate_constants(sdfg, {"N": 2, "M": 3})
    unroll_loops(sdfg)

    assert not any(isinstance(n, dace.sdfg.state.LoopRegion) for n in sdfg.all_nodes_recursive())

    body_states = [s for s in sdfg.all_states() if "body" in s.label]
    assert len(body_states) == 6

    sdfg.validate()

def test_unroll_loops_with_interstate_condition():
    sdfg = dace.SDFG("test_interstate")
    sdfg.add_symbol("N", dace.int32)

    loop = dace.sdfg.state.LoopRegion(
        "myloop",
        "i < N",
        "i",
        "i = 0",
        "i = i + 1"
    )
    sdfg.add_node(loop, is_start_block=True)

    s1 = loop.add_state("s1")
    s2 = loop.add_state("s2")
    loop.add_edge(s1, s2, dace.InterstateEdge(condition="i % 2 == 0"))
    loop.start_block = loop.node_id(s1)

    s1.add_tasklet("t1", {}, {}, "x = i")
    s2.add_tasklet("t2", {}, {}, "y = i")

    propagate_constants(sdfg, {"N": 3})
    unroll_loops(sdfg)

    found_conditions = []
    for edge in sdfg.edges():
        if isinstance(edge.data, dace.InterstateEdge) and not edge.data.is_unconditional():
            found_conditions.append(edge.data.condition.as_string)

    import re
    def normalize(s):
        return s.replace(" ", "").replace("(", "").replace(")", "")

    norm_conditions = [normalize(c) for c in found_conditions]
    assert normalize("0 % 2 == 0") in norm_conditions
    assert normalize("1 % 2 == 0") in norm_conditions
    assert normalize("2 % 2 == 0") in norm_conditions

    sdfg.validate()

def test_unroll_loops_nested_sdfg_symbol_mapping():
    sdfg = dace.SDFG("test_nested_sym")
    sdfg.add_symbol("N", dace.int32)

    loop = dace.sdfg.state.LoopRegion("myloop", "i < N", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)

    ls = loop.add_state("loop_state")

    nsdfg = dace.SDFG("nested")
    nsdfg.add_symbol("M", dace.int32)
    nstate = nsdfg.add_state("nstate")
    nstate.add_tasklet("nt", {}, {}, "y = M")

    ls.add_nested_sdfg(nsdfg, {}, {}, {"M": "i + 1"})

    propagate_constants(sdfg, {"N": 2})
    unroll_loops(sdfg)

    nested_nodes = [n for s in sdfg.all_states() for n in s.nodes() if isinstance(n, dace.sdfg.nodes.NestedSDFG)]
    assert len(nested_nodes) == 2

    mappings = [str(n.symbol_mapping["M"]) for n in nested_nodes]

    assert "1" in mappings
    assert "2" in mappings

    sdfg.validate()

def test_unroll_renames_local_scalars():
    """Each unrolled copy should get unique names for transient scalars."""
    sdfg = dace.SDFG("test_rename_scalars")
    sdfg.add_symbol("N", dace.int32)
    sdfg.add_scalar("tmp", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("myloop", "i < N", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")

    t = ls.add_tasklet("t", {}, {"o"}, "o = i")
    ls.add_edge(t, "o", ls.add_write("tmp"), None, dace.Memlet("tmp"))

    propagate_constants(sdfg, {"N": 3})
    unroll_loops(sdfg)

    an_names = set()
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, dace.nodes.AccessNode):
                an_names.add(node.data)

    tmp_names = sorted(n for n in an_names if n.startswith("tmp"))
    assert len(tmp_names) == 3, f"Expected 3 unique tmp names, got {tmp_names}"
    assert len(set(tmp_names)) == 3, f"Names not unique: {tmp_names}"
    assert "tmp" not in tmp_names, f"Original 'tmp' still in use: {tmp_names}"

    sdfg.validate()


def test_unroll_preserves_read_write_scalar():
    """Scalar read+written in loop body must NOT be renamed (loop-carried dep)."""
    sdfg = dace.SDFG("test_rw_scalar")
    sdfg.add_symbol("N", dace.int32)
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("myloop", "i < N", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")

    t = ls.add_tasklet("t", {"inp"}, {"out"}, "out = inp + 1")
    ls.add_edge(ls.add_read("s"), None, t, "inp", dace.Memlet("s"))
    ls.add_edge(t, "out", ls.add_write("s"), None, dace.Memlet("s"))

    propagate_constants(sdfg, {"N": 2})
    unroll_loops(sdfg)

    an_names = set()
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, dace.nodes.AccessNode):
                an_names.add(node.data)

    # 's' is read+written → loop-carried, must keep original name
    assert "s" in an_names, f"Loop-carried scalar 's' was renamed: {an_names}"

    sdfg.validate()

def test_unroll_preserves_escaping_scalar():
    """Scalar written in loop and read after loop must NOT be renamed."""
    sdfg = dace.SDFG("test_escape")
    sdfg.add_scalar("result", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("myloop", "i < 3", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    t = ls.add_tasklet("t", {}, {"o"}, "o = 42")
    ls.add_edge(t, "o", ls.add_write("result"), None, dace.Memlet("result"))

    # Read 'result' after the loop
    after = sdfg.add_state("after")
    sdfg.add_edge(loop, after, dace.InterstateEdge())
    t2 = after.add_tasklet("use", {"inp"}, {}, "x = inp")
    after.add_edge(after.add_read("result"), None, t2, "inp", dace.Memlet("result"))

    unroll_loops(sdfg)

    # 'result' should still be written by the last unrolled copy
    an_names = set()
    for state in sdfg.all_states():
        for node in state.nodes():
            if isinstance(node, dace.nodes.AccessNode):
                an_names.add(node.data)

    assert "result" in an_names, f"Escaping scalar 'result' was renamed away: {an_names}"

    sdfg.validate()


def test_unroll_respects_max_iterations():
    """Loop with more iterations than max_iterations should not be unrolled."""
    sdfg = dace.SDFG("test_max_iter")

    loop = dace.sdfg.state.LoopRegion("myloop", "i < 20", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    ls = loop.add_state("loop_state")
    ls.add_tasklet("lt", {}, {}, "x = i")

    # 20 iterations > default max_iterations=10
    unroll_loops(sdfg)
    assert any(isinstance(n, dace.sdfg.state.LoopRegion) for n in sdfg.nodes())

    # But with higher threshold it should unroll
    unroll_loops(sdfg, max_iterations=20)
    assert not any(isinstance(n, dace.sdfg.state.LoopRegion) for n in sdfg.nodes())

    sdfg.validate()


def test_unroll_numerical_scalar_in_interstate_edge():
    """Numerical test: private scalar referenced in interstate edge condition."""
    sdfg = dace.SDFG("test_num_edge")
    sdfg.add_array("out", [3], dace.float64)
    sdfg.add_scalar("flag", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("myloop", "i < 3", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)

    # State 1: flag = i (write-only in loop, private)
    s1 = loop.add_state("compute")
    t1 = s1.add_tasklet("comp", {}, {"o"}, "o = i")
    s1.add_edge(t1, "o", s1.add_write("flag"), None, dace.Memlet("flag"))

    # State 2: out[i] = 1.0 (reached only if flag > 0 via interstate edge)
    s2 = loop.add_state("store_yes")
    t2 = s2.add_tasklet("store", {}, {"o"}, "o = 1.0")
    s2.add_edge(t2, "o", s2.add_write("out"), None, dace.Memlet("out[i]"))

    # State 3: out[i] = -1.0 (reached if flag <= 0)
    s3 = loop.add_state("store_no")
    t3 = s3.add_tasklet("store", {}, {"o"}, "o = -1.0")
    s3.add_edge(t3, "o", s3.add_write("out"), None, dace.Memlet("out[i]"))

    loop.add_edge(s1, s2, dace.InterstateEdge(condition="flag > 0"))
    loop.add_edge(s1, s3, dace.InterstateEdge(condition="flag <= 0"))
    loop.start_block = loop.node_id(s1)

    # Reference
    ref_sdfg = copy.deepcopy(sdfg)
    ref_out = np.zeros(3)
    ref_sdfg(out=ref_out)

    # Unrolled
    unroll_loops(sdfg)
    sdfg.validate()
    test_out = np.zeros(3)
    sdfg(out=test_out)

    np.testing.assert_array_equal(test_out, ref_out)
    # i=0 -> flag=0 -> store_no (-1), i=1 -> flag=1 -> store_yes (1), i=2 -> flag=2 -> store_yes (1)
    np.testing.assert_array_equal(test_out, [-1.0, 1.0, 1.0])


def test_unroll_numerical_writeonly_scalar():
    """Numerical test: write-only scalar per iteration writes to output array."""
    sdfg = dace.SDFG("test_num_writeonly")
    sdfg.add_array("out", [3], dace.float64)
    sdfg.add_scalar("tmp", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("myloop", "i < 3", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)

    # State 1: tmp = i * 10
    s1 = loop.add_state("compute")
    t1 = s1.add_tasklet("compute", {}, {"o"}, "o = i * 10.0")
    s1.add_edge(t1, "o", s1.add_write("tmp"), None, dace.Memlet("tmp"))

    # State 2: out[i] = tmp
    s2 = loop.add_state("store")
    t2 = s2.add_tasklet("store", {"inp"}, {"o"}, "o = inp")
    s2.add_edge(s2.add_read("tmp"), None, t2, "inp", dace.Memlet("tmp"))
    s2.add_edge(t2, "o", s2.add_write("out"), None, dace.Memlet("out[i]"))
    loop.add_edge(s1, s2, dace.InterstateEdge())
    loop.start_block = loop.node_id(s1)

    # Run without unroll (reference)
    ref_sdfg = copy.deepcopy(sdfg)
    ref_out = np.zeros(3)
    ref_sdfg(out=ref_out)

    # Run with unroll
    unroll_loops(sdfg)
    sdfg.validate()
    test_out = np.zeros(3)
    sdfg(out=test_out)

    np.testing.assert_array_equal(test_out, ref_out)
    np.testing.assert_array_equal(test_out, [0.0, 10.0, 20.0])


def test_unroll_numerical_loop_carried():
    """Numerical test: loop-carried scalar accumulates across iterations."""
    sdfg = dace.SDFG("test_num_carried")
    sdfg.add_array("out", [1], dace.float64)
    sdfg.add_scalar("acc", dace.float64, transient=True)

    # init: acc = 0
    init = sdfg.add_state("init")
    t_init = init.add_tasklet("init", {}, {"o"}, "o = 0.0")
    init.add_edge(t_init, "o", init.add_write("acc"), None, dace.Memlet("acc"))

    loop = dace.sdfg.state.LoopRegion("myloop", "i < 4", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop)
    sdfg.add_edge(init, loop, dace.InterstateEdge())

    # loop body: acc = acc + i
    ls = loop.add_state("body")
    t = ls.add_tasklet("add", {"inp"}, {"o"}, "o = inp + i")
    ls.add_edge(ls.add_read("acc"), None, t, "inp", dace.Memlet("acc"))
    ls.add_edge(t, "o", ls.add_write("acc"), None, dace.Memlet("acc"))

    # after loop: out[0] = acc
    after = sdfg.add_state("after")
    sdfg.add_edge(loop, after, dace.InterstateEdge())
    t_out = after.add_tasklet("out", {"inp"}, {"o"}, "o = inp")
    after.add_edge(after.add_read("acc"), None, t_out, "inp", dace.Memlet("acc"))
    after.add_edge(t_out, "o", after.add_write("out"), None, dace.Memlet("out[0]"))

    # Reference
    ref_sdfg = copy.deepcopy(sdfg)
    ref_out = np.zeros(1)
    ref_sdfg(out=ref_out)

    # Unrolled
    unroll_loops(sdfg)
    sdfg.validate()
    test_out = np.zeros(1)
    sdfg(out=test_out)

    np.testing.assert_array_equal(test_out, ref_out)
    np.testing.assert_array_equal(test_out, [6.0])  # 0+1+2+3


def test_unroll_numerical_two_scalars():
    """Numerical test: one write-only and one loop-carried scalar in the same loop."""
    sdfg = dace.SDFG("test_num_two")
    sdfg.add_array("out", [3], dace.float64)
    sdfg.add_array("total", [1], dace.float64)
    sdfg.add_scalar("tmp", dace.float64, transient=True)  # write-only per iter
    sdfg.add_scalar("acc", dace.float64, transient=True)  # loop-carried

    # init: acc = 0
    init = sdfg.add_state("init")
    t_init = init.add_tasklet("init", {}, {"o"}, "o = 0.0")
    init.add_edge(t_init, "o", init.add_write("acc"), None, dace.Memlet("acc"))

    loop = dace.sdfg.state.LoopRegion("myloop", "i < 3", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop)
    sdfg.add_edge(init, loop, dace.InterstateEdge())

    # State 1: tmp = (i+1)*10; acc = acc + (i+1)
    s1 = loop.add_state("compute")
    t1 = s1.add_tasklet("comp_tmp", {}, {"o"}, "o = (i + 1) * 10.0")
    s1.add_edge(t1, "o", s1.add_write("tmp"), None, dace.Memlet("tmp"))
    t2 = s1.add_tasklet("comp_acc", {"inp"}, {"o"}, "o = inp + (i + 1)")
    s1.add_edge(s1.add_read("acc"), None, t2, "inp", dace.Memlet("acc"))
    s1.add_edge(t2, "o", s1.add_write("acc"), None, dace.Memlet("acc"))

    # State 2: out[i] = tmp
    s2 = loop.add_state("store")
    t3 = s2.add_tasklet("store", {"inp"}, {"o"}, "o = inp")
    s2.add_edge(s2.add_read("tmp"), None, t3, "inp", dace.Memlet("tmp"))
    s2.add_edge(t3, "o", s2.add_write("out"), None, dace.Memlet("out[i]"))
    loop.add_edge(s1, s2, dace.InterstateEdge())
    loop.start_block = loop.node_id(s1)

    # after loop: total[0] = acc
    after = sdfg.add_state("after")
    sdfg.add_edge(loop, after, dace.InterstateEdge())
    t_out = after.add_tasklet("out", {"inp"}, {"o"}, "o = inp")
    after.add_edge(after.add_read("acc"), None, t_out, "inp", dace.Memlet("acc"))
    after.add_edge(t_out, "o", after.add_write("total"), None, dace.Memlet("total[0]"))

    # Reference
    ref_sdfg = copy.deepcopy(sdfg)
    ref_out = np.zeros(3)
    ref_total = np.zeros(1)
    ref_sdfg(out=ref_out, total=ref_total)

    # Unrolled
    unroll_loops(sdfg)
    sdfg.validate()
    test_out = np.zeros(3)
    test_total = np.zeros(1)
    sdfg(out=test_out, total=test_total)

    np.testing.assert_array_equal(test_out, ref_out)
    np.testing.assert_array_equal(test_total, ref_total)
    np.testing.assert_array_equal(test_out, [10.0, 20.0, 30.0])
    np.testing.assert_array_equal(test_total, [6.0])  # 1+2+3


if __name__ == "__main__":
    test_unroll_loops_basic()
    test_unroll_loops_no_match()
    test_unroll_loops_nested()
    test_unroll_loops_complex_range()
    test_unroll_loops_multi_round()
    test_unroll_loops_with_interstate_condition()
    test_unroll_loops_nested_sdfg_symbol_mapping()
    test_unroll_renames_local_scalars()
    test_unroll_preserves_read_write_scalar()
    test_unroll_preserves_escaping_scalar()
    test_unroll_respects_max_iterations()
    test_unroll_numerical_scalar_in_interstate_edge()
    test_unroll_numerical_writeonly_scalar()
    test_unroll_numerical_loop_carried()
    test_unroll_numerical_two_scalars()
    print("All tests passed!")
