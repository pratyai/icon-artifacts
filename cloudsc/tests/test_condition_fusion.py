"""Tests for condition_fusion.py"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dace
from dace.sdfg.state import LoopRegion, ConditionalBlock, ControlFlowRegion, SDFGState
from dace.properties import CodeBlock

from ssa.condition_fusion import fuse_conditions_in_loop, fuse_all_conditions, _get_linear_chain


def _make_loop_with_one_cb():
    """Loop: S1 → CB(cond_a: body_a, else: body_else) → S2"""
    sdfg = dace.SDFG("test_one_cb")
    sdfg.add_scalar("x", dace.float64, transient=True)

    loop = LoopRegion("loop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)

    s1 = loop.add_state("s1", is_start_block=True)
    s2 = loop.add_state("s2")

    cb = ConditionalBlock("if_test")

    # Branch A: write x = 1
    br_a = ControlFlowRegion("br_a", sdfg=sdfg)
    st_a = br_a.add_state("st_a", is_start_block=True)
    t_a = st_a.add_tasklet("t_a", {}, {"o"}, "o = 1.0")
    st_a.add_edge(t_a, "o", st_a.add_write("x"), None, dace.Memlet("x"))
    cb.add_branch(CodeBlock("i > 5"), br_a)

    # Else: write x = 2
    br_else = ControlFlowRegion("br_else", sdfg=sdfg)
    st_else = br_else.add_state("st_else", is_start_block=True)
    t_else = st_else.add_tasklet("t_else", {}, {"o"}, "o = 2.0")
    st_else.add_edge(t_else, "o", st_else.add_write("x"), None, dace.Memlet("x"))
    cb.add_branch(None, br_else)

    loop.add_node(cb)
    loop.add_edge(s1, cb, dace.InterstateEdge())
    loop.add_edge(cb, s2, dace.InterstateEdge())

    return sdfg, loop


def _make_loop_with_two_cbs():
    """Loop: S1 → CB1(a: ba, else: be1) → S2 → CB2(b: bb, else: be2) → S3"""
    sdfg = dace.SDFG("test_two_cbs")
    sdfg.add_scalar("x", dace.float64, transient=True)
    sdfg.add_scalar("y", dace.float64, transient=True)

    loop = LoopRegion("loop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)

    s1 = loop.add_state("s1", is_start_block=True)
    s2 = loop.add_state("s2")
    s3 = loop.add_state("s3")

    # CB1
    cb1 = ConditionalBlock("if_1")
    br1_a = ControlFlowRegion("br1_a", sdfg=sdfg)
    st1_a = br1_a.add_state("st1_a", is_start_block=True)
    t1 = st1_a.add_tasklet("t1a", {}, {"o"}, "o = 1.0")
    st1_a.add_edge(t1, "o", st1_a.add_write("x"), None, dace.Memlet("x"))
    cb1.add_branch(CodeBlock("i > 5"), br1_a)

    br1_else = ControlFlowRegion("br1_else", sdfg=sdfg)
    st1_else = br1_else.add_state("st1_else", is_start_block=True)
    t1e = st1_else.add_tasklet("t1e", {}, {"o"}, "o = 0.0")
    st1_else.add_edge(t1e, "o", st1_else.add_write("x"), None, dace.Memlet("x"))
    cb1.add_branch(None, br1_else)

    # CB2
    cb2 = ConditionalBlock("if_2")
    br2_a = ControlFlowRegion("br2_a", sdfg=sdfg)
    st2_a = br2_a.add_state("st2_a", is_start_block=True)
    t2 = st2_a.add_tasklet("t2a", {}, {"o"}, "o = 10.0")
    st2_a.add_edge(t2, "o", st2_a.add_write("y"), None, dace.Memlet("y"))
    cb2.add_branch(CodeBlock("i < 3"), br2_a)

    br2_else = ControlFlowRegion("br2_else", sdfg=sdfg)
    st2_else = br2_else.add_state("st2_else", is_start_block=True)
    t2e = st2_else.add_tasklet("t2e", {}, {"o"}, "o = 20.0")
    st2_else.add_edge(t2e, "o", st2_else.add_write("y"), None, dace.Memlet("y"))
    cb2.add_branch(None, br2_else)

    loop.add_node(cb1)
    loop.add_node(cb2)
    loop.add_edge(s1, cb1, dace.InterstateEdge())
    loop.add_edge(cb1, s2, dace.InterstateEdge())
    loop.add_edge(s2, cb2, dace.InterstateEdge())
    loop.add_edge(cb2, s3, dace.InterstateEdge())

    return sdfg, loop


def test_linear_chain_detection():
    sdfg, loop = _make_loop_with_one_cb()
    result = _get_linear_chain(loop)
    assert result is not None
    chain, edges = result
    assert len(chain) == 3  # S1, CB, S2
    assert len(edges) == 2
    assert isinstance(chain[0], SDFGState)
    assert isinstance(chain[1], ConditionalBlock)
    assert isinstance(chain[2], SDFGState)


def test_no_cb_skips():
    """Loop with only states — should not fuse."""
    sdfg = dace.SDFG("test_no_cb")
    loop = LoopRegion("loop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    s1 = loop.add_state("s1", is_start_block=True)
    s2 = loop.add_state("s2")
    loop.add_edge(s1, s2, dace.InterstateEdge())

    assert not fuse_conditions_in_loop(loop, sdfg)
    assert len(list(loop.nodes())) == 2  # unchanged


def test_non_linear_skips():
    """Loop with a diamond — should not fuse."""
    sdfg = dace.SDFG("test_diamond")
    loop = LoopRegion("loop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)
    s1 = loop.add_state("s1", is_start_block=True)
    s2 = loop.add_state("s2")
    s3 = loop.add_state("s3")
    s4 = loop.add_state("s4")
    loop.add_edge(s1, s2, dace.InterstateEdge())
    loop.add_edge(s1, s3, dace.InterstateEdge())
    loop.add_edge(s2, s4, dace.InterstateEdge())
    loop.add_edge(s3, s4, dace.InterstateEdge())

    assert not fuse_conditions_in_loop(loop, sdfg)


def test_one_cb_fusion():
    """Loop with 1 CB → result should have 1 giant CB with 2 branches."""
    sdfg, loop = _make_loop_with_one_cb()
    assert fuse_conditions_in_loop(loop, sdfg)

    # Should have exactly one child: the giant CB
    children = list(loop.nodes())
    assert len(children) == 1
    giant_cb = children[0]
    assert isinstance(giant_cb, ConditionalBlock)

    # 2 branches (one condition + one else)
    assert len(giant_cb.branches) == 2

    # First branch has explicit condition, last is else (None)
    assert giant_cb.branches[0][0] is not None
    assert giant_cb.branches[1][0] is None

    # Each branch body should contain S1 + branch body + S2 = 3 states
    for _, cfr in giant_cb.branches:
        states = list(cfr.nodes())
        assert len(states) == 3, f"Expected 3 states, got {len(states)}: {[s.label for s in states]}"


def test_two_cb_fusion():
    """Loop with 2 CBs → result should have 1 giant CB with 4 branches."""
    sdfg, loop = _make_loop_with_two_cbs()
    assert fuse_conditions_in_loop(loop, sdfg)

    children = list(loop.nodes())
    assert len(children) == 1
    giant_cb = children[0]
    assert isinstance(giant_cb, ConditionalBlock)

    # 2 CBs × 2 branches each = 4 combinations
    assert len(giant_cb.branches) == 4

    # Last branch is else
    assert giant_cb.branches[3][0] is None

    # Each branch body: S1 + branch1_body + S2 + branch2_body + S3 = 5 nodes
    for _, cfr in giant_cb.branches:
        nodes = list(cfr.nodes())
        assert len(nodes) == 5, f"Expected 5 nodes, got {len(nodes)}: {[n.label for n in nodes]}"


def test_edge_data_preserved():
    """Edge assignments between chain nodes should be preserved in branch bodies."""
    sdfg = dace.SDFG("test_edges")
    sdfg.add_scalar("x", dace.float64, transient=True)
    sdfg.add_symbol("flag", dace.int32)

    loop = LoopRegion("loop", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop, is_start_block=True)

    s1 = loop.add_state("s1", is_start_block=True)
    s2 = loop.add_state("s2")

    cb = ConditionalBlock("if_test")
    br = ControlFlowRegion("br", sdfg=sdfg)
    br.add_state("st_br", is_start_block=True)
    cb.add_branch(CodeBlock("flag > 0"), br)
    br_else = ControlFlowRegion("br_else", sdfg=sdfg)
    br_else.add_state("st_else", is_start_block=True)
    cb.add_branch(None, br_else)

    loop.add_node(cb)
    # Edge with assignment
    loop.add_edge(s1, cb, dace.InterstateEdge(assignments={"flag": "1"}))
    loop.add_edge(cb, s2, dace.InterstateEdge())

    assert fuse_conditions_in_loop(loop, sdfg)

    giant_cb = list(loop.nodes())[0]
    # Check that the assignment is in the branch bodies
    for _, cfr in giant_cb.branches:
        edges_with_assign = [e for e in cfr.edges() if e.data.assignments]
        assert len(edges_with_assign) >= 1, "Edge assignment was lost"
        assert "flag" in edges_with_assign[0].data.assignments


def test_fuse_all_conditions():
    """Test the top-level fuse_all_conditions function."""
    sdfg, _ = _make_loop_with_two_cbs()
    n = fuse_all_conditions(sdfg)
    assert n == 1


if __name__ == "__main__":
    test_linear_chain_detection()
    test_no_cb_skips()
    test_non_linear_skips()
    test_one_cb_fusion()
    test_two_cb_fusion()
    test_edge_data_preserved()
    test_fuse_all_conditions()
    print("All condition fusion tests passed!")
