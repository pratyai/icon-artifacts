import dace
import pytest
import sympy as sp
from ssa.graph_utils import (
    rename_access_node_and_tree, update_node_content, update_interstate_edge,
    update_metadata, collect_memlet_tree_edges, is_write, is_read, get_enclosing_loop,
    rename_map_connectors, rename_nested_sdfg_connectors, rename_memlet_data,
    topological_sort, get_direct_child, update_conditional_metadata, update_loop_metadata,
    collect_all_loops, loop_interior, written_transient_scalars, writeonly_transient_scalars,
    loop_carried_scalars, rename_local_scalars, all_nodes_in_block,
    lift_data_refs_in_conditions
)

def test_memlet_tree_collection():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    # an1 -> t1 -> s[0] -> an2
    an1 = state.add_access("s")
    t1 = state.add_tasklet("t1", {"i"}, {"o"}, "o = i")
    an2 = state.add_access("s")
    
    state.add_edge(an1, None, t1, "i", dace.Memlet("s[0]"))
    state.add_edge(t1, "o", an2, None, dace.Memlet("s[0]"))
    
    edges1 = collect_memlet_tree_edges(state, an1)
    assert len(edges1) == 1
    
    edges2 = collect_memlet_tree_edges(state, an2)
    assert len(edges2) == 1

def test_is_read_write():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    sdfg.add_scalar("s", dace.float64, transient=True)
    an = state.add_access("s")
    
    assert not is_read(state, an)
    assert not is_write(state, an)
    
    t = state.add_tasklet("t", {"i"}, {"o"}, "o=i")
    state.add_edge(an, None, t, "i", dace.Memlet("s[0]"))
    assert is_read(state, an)
    assert not is_write(state, an)
    
    state.add_edge(t, "o", an, None, dace.Memlet("s[0]"))
    assert is_read(state, an)
    assert is_write(state, an)

def test_rename_basic():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    sdfg.add_scalar("s", dace.float64, transient=True)
    an = state.add_access("s")
    rename_access_node_and_tree(state, an, "s", "s_new")
    assert an.data == "s_new"

def test_rename_map_subroutine():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    sdfg.add_scalar("s", dace.float64, transient=True)
    ani = state.add_access("s")
    me, mx = state.add_map("m", dict(i="0:10"))
    t = state.add_tasklet("t", {"in1"}, {"out1"}, "out1 = in1")
    ano = state.add_access("s")
    
    state.add_memlet_path(ani, me, t, dst_conn="in1", memlet=dace.Memlet("s[0]"))
    state.add_memlet_path(t, mx, ano, src_conn="out1", memlet=dace.Memlet("s[0]"))
    
    edges_to_rename = collect_memlet_tree_edges(state, ani)
    rename_map_connectors(state, "s", "s_in", edges_to_rename)
    
    assert "IN_s_in" in me.in_connectors
    assert "OUT_s_in" in me.out_connectors
    assert state.in_edges(me)[0].dst_conn == "IN_s_in"
    assert state.out_edges(me)[0].src_conn == "OUT_s_in"

def test_rename_map_exit_subroutine():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    sdfg.add_scalar("s", dace.float64, transient=True)
    ani = state.add_access("s")
    me, mx = state.add_map("m", dict(i="0:10"))
    t = state.add_tasklet("t", {"in1"}, {"out1"}, "out1 = in1")
    ano = state.add_access("s")
    
    state.add_memlet_path(ani, me, t, dst_conn="in1", memlet=dace.Memlet("s[0]"))
    state.add_memlet_path(t, mx, ano, src_conn="out1", memlet=dace.Memlet("s[0]"))
    
    edges_to_rename = collect_memlet_tree_edges(state, ano)
    rename_map_connectors(state, "s", "s_out", edges_to_rename)
    
    assert "IN_s_out" in mx.in_connectors
    assert "OUT_s_out" in mx.out_connectors
    assert state.in_edges(mx)[0].dst_conn == "IN_s_out"
    assert state.out_edges(mx)[0].src_conn == "OUT_s_out"

def test_rename_nested_sdfg_subroutine():
    sdfg = dace.SDFG("parent")
    state = sdfg.add_state("s")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    nsdfg = dace.SDFG("child")
    nsdfg.add_scalar("s", dace.float64, transient=False)
    cs = nsdfg.add_state("cs")
    cb = dace.sdfg.state.ConditionalBlock("cb")
    nsdfg.add_node(cb)
    nsdfg.add_edge(cs, cb, dace.InterstateEdge())
    branch = dace.sdfg.state.ControlFlowRegion("branch", sdfg=nsdfg)
    branch.add_state("bs")
    cb.add_branch("s > 0", branch)
    
    nn = state.add_nested_sdfg(nsdfg, {"s"}, set(), {})
    an = state.add_access("s")
    state.add_edge(an, None, nn, "s", dace.Memlet("s[0]"))
    
    edges_to_rename = collect_memlet_tree_edges(state, an)
    rename_nested_sdfg_connectors(state, "s", "s_ext", edges_to_rename)
    
    assert "s_ext" in nn.in_connectors
    assert "s_ext" in nsdfg.arrays
    assert "s_ext > 0" in cb.branches[0][0].as_string

def test_rename_nested_sdfg_out_connector():
    sdfg = dace.SDFG("parent")
    state = sdfg.add_state("ls")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    nsdfg = dace.SDFG("child")
    nsdfg.add_scalar("s", dace.float64, transient=False)
    cs = nsdfg.add_state("cs")
    t = cs.add_tasklet("t", {}, {"o"}, "o = 1.0")
    cs.add_edge(t, "o", cs.add_write("s"), None, dace.Memlet("s[0]"))
    
    nn = state.add_nested_sdfg(nsdfg, set(), {"s"}, {})
    an = state.add_access("s")
    state.add_edge(nn, "s", an, None, dace.Memlet("s[0]"))
    
    edges_to_rename = collect_memlet_tree_edges(state, an)
    rename_nested_sdfg_connectors(state, "s", "s_ext", edges_to_rename)
    
    assert "s_ext" in nn.out_connectors
    assert "s_ext" in nsdfg.arrays
    assert "s" not in nn.out_connectors

def test_rename_memlet_data():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    sdfg.add_scalar("s", dace.float64, transient=True)
    an = state.add_access("s")
    t = state.add_tasklet("t", {"i"}, {}, "x = i")
    e = state.add_edge(an, None, t, "i", dace.Memlet("s[0]"))
    
    edges_to_rename = {e}
    rename_memlet_data(state, "s", "s_new", edges_to_rename)
    assert e.data.data == "s_new"

def test_update_tasklet():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    t = state.add_tasklet("t", {}, {}, "s = 1.0")
    update_node_content(t, {"s": "s_new"})
    assert "s_new = 1.0" in t.code.as_string

def test_update_nested_sdfg():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    nsdfg = dace.SDFG("child")
    nnode = state.add_nested_sdfg(nsdfg, set(), set(), {"s_int": dace.symbol("s_ext")})
    update_node_content(nnode, {"s_ext": "s_new"})
    assert str(nnode.symbol_mapping["s_int"]) == "s_new"

def test_update_nested_sdfg_expr():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    nsdfg = dace.SDFG("child")
    nnode = state.add_nested_sdfg(nsdfg, set(), set(), {"s_int": dace.symbolic.pystr_to_symbolic("s_ext + 1")})
    update_node_content(nnode, {"s_ext": "s_new"})
    assert str(nnode.symbol_mapping["s_int"]) == "s_new + 1"

def test_update_interstate_edge():
    edge = dace.InterstateEdge(condition="s > 0", assignments={"x": "s + 1"})
    update_interstate_edge(edge, {"s": "s_new"})
    assert "s_new > 0" in edge.condition.as_string
    assert edge.assignments["x"] == "s_new + 1"

def test_update_conditional_metadata():
    sdfg = dace.SDFG("test")
    cb = dace.sdfg.state.ConditionalBlock("cb")
    cb.add_branch("s > 0", dace.sdfg.state.ControlFlowRegion("b1", sdfg=sdfg))
    update_conditional_metadata(cb, {"s": "s_new"})
    assert "s_new > 0" in cb.branches[0][0].as_string

def test_update_loop_metadata():
    sdfg = dace.SDFG("test")
    loop = dace.sdfg.state.LoopRegion("l", "i < s", "i", "i = 0", "i = i + 1")
    update_loop_metadata(loop, {"s": "s_new"})
    assert "i < s_new" in loop.loop_condition.as_string

def test_update_metadata_recursive():
    sdfg = dace.SDFG("test")
    outer = dace.sdfg.state.ControlFlowRegion("outer", sdfg=sdfg)
    loop = dace.sdfg.state.LoopRegion("l", "i < s", "i", "i = 0", "i = i + 1")
    outer.add_node(loop)
    
    update_metadata(outer, {"s": "s_new"}, recursive=True)
    assert "i < s_new" in loop.loop_condition.as_string

def test_get_enclosing_loop():
    sdfg = dace.SDFG("test")
    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop)
    ls = loop.add_state("ls")
    
    assert get_enclosing_loop(ls) == loop
    
    cb = dace.sdfg.state.ConditionalBlock("cb")
    loop.add_node(cb)
    branch = dace.sdfg.state.ControlFlowRegion("b", sdfg=sdfg)
    cb.add_branch("True", branch)
    bs = branch.add_state("bs")
    
    assert get_enclosing_loop(bs) == loop

def test_get_enclosing_loop_nested_sdfg():
    sdfg = dace.SDFG("parent")
    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i = 0", "i = i + 1")
    sdfg.add_node(loop)
    ls = loop.add_state("ls")
    
    nsdfg = dace.SDFG("child")
    cs = nsdfg.add_state("cs")
    nnode = ls.add_nested_sdfg(nsdfg, {}, {}, {})
    
    assert get_enclosing_loop(cs) == loop

def test_get_direct_child():
    sdfg = dace.SDFG("parent")
    outer = dace.sdfg.state.ControlFlowRegion("outer", sdfg=sdfg)
    sdfg.add_node(outer)
    
    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i = 0", "i = i + 1")
    outer.add_node(loop)
    ls = loop.add_state("ls")
    
    assert get_direct_child(outer, ls) == loop
    assert get_direct_child(sdfg, ls) == outer
    assert get_direct_child(loop, ls) == ls
    assert get_direct_child(sdfg, loop) == outer

def test_topological_sort_cyclic():
    sdfg = dace.SDFG("test")
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    sdfg.add_edge(s2, s1, dace.InterstateEdge())
    
    nodes = topological_sort(sdfg)
    assert len(nodes) == 2
    assert s1 in nodes
    assert s2 in nodes

def test_topological_sort_error():
    with pytest.raises(TypeError):
        topological_sort("not a graph")

def test_update_node_content_map():
    sdfg = dace.SDFG("test")
    state = sdfg.add_state("s")
    me, mx = state.add_map("m", dict(i="0:s"))
    
    update_node_content(me, {"s": "s_new"})
    assert str(me.map.range[0][1]) == "s_new - 1"

def test_update_interstate_edge_lhs():
    edge = dace.InterstateEdge(assignments={"s": "1"})
    update_interstate_edge(edge, {"s": "s_v1"})
    assert "s_v1" in edge.assignments
    assert "s" not in edge.assignments

def test_collect_all_loops():
    sdfg = dace.SDFG("test_collect")
    s1 = sdfg.add_state("s1")

    outer = dace.sdfg.state.LoopRegion("outer", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(outer)
    sdfg.add_edge(s1, outer, dace.InterstateEdge())

    inner = dace.sdfg.state.LoopRegion("inner", "j < 5", "j", "j=0", "j=j+1")
    outer.add_node(inner, is_start_block=True)
    inner.add_state("body")

    loops = collect_all_loops(sdfg)
    labels = {l.label for l in loops}
    assert labels == {"outer", "inner"}


def test_collect_all_loops_empty():
    sdfg = dace.SDFG("test_no_loops")
    sdfg.add_state("s1")
    assert collect_all_loops(sdfg) == []


def test_loop_interior():
    sdfg = dace.SDFG("test_interior")
    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    s1 = loop.add_state("s1")
    s2 = loop.add_state("s2")
    loop.add_edge(s1, s2, dace.InterstateEdge())

    states, blocks, edges = loop_interior(loop)
    assert s1 in states
    assert s2 in states
    assert len(edges) == 1  # the interstate edge between s1 and s2


def test_loop_interior_nested():
    sdfg = dace.SDFG("test_interior_nested")
    outer = dace.sdfg.state.LoopRegion("outer", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(outer)
    inner = dace.sdfg.state.LoopRegion("inner", "j < 5", "j", "j=0", "j=j+1")
    outer.add_node(inner, is_start_block=True)
    body = inner.add_state("body")

    states, blocks, edges = loop_interior(outer)
    # Inner loop's state should be included in outer's interior
    assert body in states
    assert inner in blocks


def test_written_transient_scalars_basic():
    sdfg = dace.SDFG("test_written")
    sdfg.add_scalar("s", dace.float64, transient=True)
    sdfg.add_scalar("r", dace.float64, transient=True)
    sdfg.add_array("A", [10], dace.float64, transient=True)

    state = sdfg.add_state("s1")
    t = state.add_tasklet("t", {}, {"o"}, "o = 1")
    state.add_edge(t, "o", state.add_write("s"), None, dace.Memlet("s"))
    state.add_read("r")  # read-only

    written = written_transient_scalars(sdfg, [state])
    assert "s" in written
    assert "r" not in written
    assert "A" not in written  # arrays excluded


def test_written_transient_scalars_non_transient():
    sdfg = dace.SDFG("test_non_trans")
    sdfg.add_scalar("s", dace.float64, transient=False)

    state = sdfg.add_state("s1")
    t = state.add_tasklet("t", {}, {"o"}, "o = 1")
    state.add_edge(t, "o", state.add_write("s"), None, dace.Memlet("s"))

    written = written_transient_scalars(sdfg, [state])
    assert "s" not in written  # non-transient excluded


def test_writeonly_transient_scalars_basic():
    """Write-only scalars are returned; read+write scalars are excluded."""
    sdfg = dace.SDFG("test_writeonly")
    sdfg.add_scalar("wo", dace.float64, transient=True)
    sdfg.add_scalar("rw", dace.float64, transient=True)

    state = sdfg.add_state("s1")
    # wo: write-only
    t1 = state.add_tasklet("t1", {}, {"o"}, "o = 1")
    state.add_edge(t1, "o", state.add_write("wo"), None, dace.Memlet("wo"))
    # rw: read + write (loop-carried pattern)
    t2 = state.add_tasklet("t2", {"inp"}, {"out"}, "out = inp + 1")
    state.add_edge(state.add_read("rw"), None, t2, "inp", dace.Memlet("rw"))
    state.add_edge(t2, "out", state.add_write("rw"), None, dace.Memlet("rw"))

    result = writeonly_transient_scalars(sdfg, [state])
    assert "wo" in result
    assert "rw" not in result


def test_writeonly_transient_scalars_excludes_arrays():
    """Arrays should never appear in writeonly_transient_scalars."""
    sdfg = dace.SDFG("test_writeonly_arr")
    sdfg.add_array("A", [10], dace.float64, transient=True)

    state = sdfg.add_state("s1")
    t = state.add_tasklet("t", {}, {"o"}, "o = 1")
    state.add_edge(t, "o", state.add_write("A"), None, dace.Memlet("A[0]"))

    result = writeonly_transient_scalars(sdfg, [state])
    assert "A" not in result


def test_all_nodes_in_block_state():
    sdfg = dace.SDFG("test_anib_state")
    s = sdfg.add_state("s")
    t = s.add_tasklet("t", {}, {}, "x = 1")

    nodes = list(all_nodes_in_block(s))
    assert len(nodes) == 1
    assert nodes[0] == (t, s)


def test_all_nodes_in_block_loop():
    sdfg = dace.SDFG("test_anib_loop")
    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    s1 = loop.add_state("s1")
    t1 = s1.add_tasklet("t1", {}, {}, "x = 1")
    s2 = loop.add_state("s2")
    t2 = s2.add_tasklet("t2", {}, {}, "y = 2")

    nodes = list(all_nodes_in_block(loop))
    assert (t1, s1) in nodes
    assert (t2, s2) in nodes


def test_all_nodes_in_block_conditional():
    sdfg = dace.SDFG("test_anib_cond")
    cb = dace.sdfg.state.ConditionalBlock("cb")
    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    bs = branch.add_state("bs")
    t = bs.add_tasklet("t", {}, {}, "x = 1")
    cb.add_branch("True", branch)

    nodes = list(all_nodes_in_block(cb))
    assert len(nodes) == 1
    assert nodes[0] == (t, bs)


def test_rename_local_scalars():
    sdfg = dace.SDFG("test_rename_local")
    sdfg.add_scalar("tmp", dace.float64, transient=True)

    s1 = sdfg.add_state("s1")
    t = s1.add_tasklet("t", {}, {"o"}, "o = 1")
    s1.add_edge(t, "o", s1.add_write("tmp"), None, dace.Memlet("tmp"))

    rename_local_scalars(sdfg, [s1], "_copy0")

    # AccessNode should now reference tmp_copy0
    an = [n for n in s1.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    assert an.data == "tmp_copy0"
    assert "tmp_copy0" in sdfg.arrays


def test_rename_local_scalars_collision():
    sdfg = dace.SDFG("test_rename_collision")
    sdfg.add_scalar("tmp", dace.float64, transient=True)
    # Pre-register the expected name to force a collision
    sdfg.add_scalar("tmp_copy0", dace.float64, transient=True)

    s1 = sdfg.add_state("s1")
    t = s1.add_tasklet("t", {}, {"o"}, "o = 1")
    s1.add_edge(t, "o", s1.add_write("tmp"), None, dace.Memlet("tmp"))

    rename_local_scalars(sdfg, [s1], "_copy0")

    an = [n for n in s1.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    # Should NOT be "tmp" (unchanged) or "tmp_copy0" (collision)
    assert an.data != "tmp"
    assert an.data != "tmp_copy0"
    assert an.data.startswith("tmp_copy0_")
    assert an.data in sdfg.arrays


def test_rename_local_scalars_conditional_block():
    """Scalars written inside a ConditionalBlock branch must be renamed."""
    sdfg = dace.SDFG("test_rename_cond")
    sdfg.add_scalar("tmp", dace.float64, transient=True)

    cb = dace.sdfg.state.ConditionalBlock("cb")
    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    bs = branch.add_state("bs")
    t = bs.add_tasklet("t", {}, {"o"}, "o = 1")
    bs.add_edge(t, "o", bs.add_write("tmp"), None, dace.Memlet("tmp"))
    cb.add_branch("True", branch)

    rename_local_scalars(sdfg, [cb], "_copy0")

    an = [n for n in bs.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    assert an.data != "tmp", "Scalar inside ConditionalBlock was not renamed"
    assert an.data in sdfg.arrays


def test_rename_local_scalars_skips_reads():
    sdfg = dace.SDFG("test_rename_skip")
    sdfg.add_scalar("r", dace.float64, transient=True)

    s1 = sdfg.add_state("s1")
    s1.add_read("r")  # read-only, not written

    rename_local_scalars(sdfg, [s1], "_copy0")

    # Name should be unchanged — read-only scalars are not renamed
    an = [n for n in s1.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    assert an.data == "r"
    assert "r_copy0" not in sdfg.arrays


def test_loop_carried_scalars_read_before_write():
    """Scalar read before written in loop body is loop-carried."""
    sdfg = dace.SDFG("test_carried")
    sdfg.add_scalar("acc", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)

    # Single state: acc = acc + 1 (read+write in same state → loop-carried)
    ls = loop.add_state("body")
    t = ls.add_tasklet("t", {"inp"}, {"out"}, "out = inp + 1")
    ls.add_edge(ls.add_read("acc"), None, t, "inp", dace.Memlet("acc"))
    ls.add_edge(t, "out", ls.add_write("acc"), None, dace.Memlet("acc"))

    result = loop_carried_scalars(sdfg, loop)
    assert "acc" in result


def test_loop_carried_scalars_write_before_read():
    """Scalar written before read in loop body is NOT loop-carried."""
    sdfg = dace.SDFG("test_not_carried")
    sdfg.add_scalar("tmp", dace.float64, transient=True)
    sdfg.add_array("out", [10], dace.float64)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)

    # State 1: write tmp
    s1 = loop.add_state("write")
    t1 = s1.add_tasklet("t1", {}, {"o"}, "o = i * 2.0")
    s1.add_edge(t1, "o", s1.add_write("tmp"), None, dace.Memlet("tmp"))

    # State 2: read tmp → out[i]
    s2 = loop.add_state("read")
    t2 = s2.add_tasklet("t2", {"inp"}, {"o"}, "o = inp")
    s2.add_edge(s2.add_read("tmp"), None, t2, "inp", dace.Memlet("tmp"))
    s2.add_edge(t2, "o", s2.add_write("out"), None, dace.Memlet("out[i]"))

    loop.add_edge(s1, s2, dace.InterstateEdge())
    loop.start_block = loop.node_id(s1)

    result = loop_carried_scalars(sdfg, loop)
    assert "tmp" not in result, f"tmp should NOT be loop-carried, got {result}"


def test_loop_carried_scalars_mixed():
    """Loop with both carried and non-carried scalars."""
    sdfg = dace.SDFG("test_mixed")
    sdfg.add_scalar("carried", dace.float64, transient=True)
    sdfg.add_scalar("scratch", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 5", "i", "i=0", "i=i+1")
    sdfg.add_node(loop, is_start_block=True)

    # State 1: read carried (loop-carried); write scratch
    s1 = loop.add_state("s1")
    t1 = s1.add_tasklet("t1", {"c"}, {"o", "s"}, "s = c; o = c + 1")
    s1.add_edge(s1.add_read("carried"), None, t1, "c", dace.Memlet("carried"))
    s1.add_edge(t1, "o", s1.add_write("carried"), None, dace.Memlet("carried"))
    s1.add_edge(t1, "s", s1.add_write("scratch"), None, dace.Memlet("scratch"))

    # State 2: read scratch (written in s1 → not carried)
    s2 = loop.add_state("s2")
    t2 = s2.add_tasklet("t2", {"inp"}, {}, "x = inp")
    s2.add_edge(s2.add_read("scratch"), None, t2, "inp", dace.Memlet("scratch"))

    loop.add_edge(s1, s2, dace.InterstateEdge())
    loop.start_block = loop.node_id(s1)

    result = loop_carried_scalars(sdfg, loop)
    assert "carried" in result
    assert "scratch" not in result, f"scratch should NOT be carried, got {result}"


def test_lift_data_refs_basic():
    """Scalar data ref in condition gets lifted to a symbol with incoming edge assignment."""
    sdfg = dace.SDFG("test_lift")
    sdfg.add_scalar("s", dace.float64, transient=True)

    init = sdfg.add_state("init")
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())

    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("s > 0", branch)
    cb.add_branch(None, dace.sdfg.state.ControlFlowRegion("br2", sdfg=sdfg))

    lift_data_refs_in_conditions(sdfg)

    # Condition should no longer reference 's' directly
    cond_str = cb.branches[0][0].as_string
    assert "s" not in cond_str.split("__")[0] or "__s_cond" in cond_str
    assert "__s_cond" in cond_str

    # Symbol should exist
    assert "__s_cond" in sdfg.symbols

    # Incoming edge should carry the assignment
    in_edges = list(sdfg.in_edges(cb))
    assert len(in_edges) == 1
    assert "__s_cond" in in_edges[0].data.assignments
    assert in_edges[0].data.assignments["__s_cond"] == "s"


def test_lift_data_refs_no_incoming_edge():
    """When ConditionalBlock is the first block, a state is inserted before it."""
    sdfg = dace.SDFG("test_lift_no_edge")
    sdfg.add_scalar("x", dace.float64, transient=True)

    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb, is_start_block=True)

    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("x > 0", branch)

    lift_data_refs_in_conditions(sdfg)

    # A new state should have been added before cb
    in_edges = list(sdfg.in_edges(cb))
    assert len(in_edges) == 1
    assert "__x_cond" in in_edges[0].data.assignments

    cond_str = cb.branches[0][0].as_string
    assert "__x_cond" in cond_str


def test_lift_data_refs_ignores_symbols():
    """Symbols already in conditions should not be lifted."""
    sdfg = dace.SDFG("test_lift_sym")
    sdfg.add_symbol("n", dace.int32)

    init = sdfg.add_state("init")
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())

    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("n > 0", branch)

    lift_data_refs_in_conditions(sdfg)

    # Condition should be unchanged (n is a symbol, not data)
    assert "n > 0" in cb.branches[0][0].as_string
    # No new symbols should have been created
    assert "__n_cond" not in sdfg.symbols


def test_lift_data_refs_ignores_arrays():
    """Array data descriptors in conditions should not be lifted (only scalars)."""
    sdfg = dace.SDFG("test_lift_arr")
    sdfg.add_array("A", [10], dace.float64, transient=True)

    init = sdfg.add_state("init")
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())

    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("A > 0", branch)

    lift_data_refs_in_conditions(sdfg)

    # Condition should be unchanged (A is an array, not a scalar)
    assert "A > 0" in cb.branches[0][0].as_string


def test_lift_data_refs_multiple_scalars():
    """Multiple scalar refs in a single condition are all lifted."""
    sdfg = dace.SDFG("test_lift_multi")
    sdfg.add_scalar("a", dace.float64, transient=True)
    sdfg.add_scalar("b", dace.float64, transient=True)

    init = sdfg.add_state("init")
    cb = dace.sdfg.state.ConditionalBlock("cb")
    sdfg.add_node(cb)
    sdfg.add_edge(init, cb, dace.InterstateEdge())

    branch = dace.sdfg.state.ControlFlowRegion("br", sdfg=sdfg)
    branch.add_state("bs")
    cb.add_branch("a > b", branch)

    lift_data_refs_in_conditions(sdfg)

    cond_str = cb.branches[0][0].as_string
    assert "__a_cond" in cond_str
    assert "__b_cond" in cond_str
    assert "__a_cond" in sdfg.symbols
    assert "__b_cond" in sdfg.symbols

    in_edge = list(sdfg.in_edges(cb))[0]
    assert in_edge.data.assignments["__a_cond"] == "a"
    assert in_edge.data.assignments["__b_cond"] == "b"


if __name__ == "__main__":
    test_memlet_tree_collection()
    test_is_read_write()
    test_rename_basic()
    test_rename_map_subroutine()
    test_rename_map_exit_subroutine()
    test_rename_nested_sdfg_subroutine()
    test_rename_nested_sdfg_out_connector()
    test_rename_memlet_data()
    test_update_tasklet()
    test_update_nested_sdfg()
    test_update_nested_sdfg_expr()
    test_update_node_content_map()
    test_update_interstate_edge()
    test_update_interstate_edge_lhs()
    test_update_conditional_metadata()
    test_update_loop_metadata()
    test_update_metadata_recursive()
    test_get_enclosing_loop()
    test_get_enclosing_loop_nested_sdfg()
    test_get_direct_child()
    test_topological_sort_cyclic()
    test_topological_sort_error()
    test_collect_all_loops()
    test_collect_all_loops_empty()
    test_loop_interior()
    test_loop_interior_nested()
    test_written_transient_scalars_basic()
    test_written_transient_scalars_non_transient()
    test_writeonly_transient_scalars_basic()
    test_writeonly_transient_scalars_excludes_arrays()
    test_all_nodes_in_block_state()
    test_all_nodes_in_block_loop()
    test_all_nodes_in_block_conditional()
    test_rename_local_scalars()
    test_rename_local_scalars_collision()
    test_rename_local_scalars_conditional_block()
    test_rename_local_scalars_skips_reads()
    test_loop_carried_scalars_read_before_write()
    test_loop_carried_scalars_write_before_read()
    test_loop_carried_scalars_mixed()
    test_lift_data_refs_basic()
    test_lift_data_refs_no_incoming_edge()
    test_lift_data_refs_ignores_symbols()
    test_lift_data_refs_ignores_arrays()
    test_lift_data_refs_multiple_scalars()
    print("All graph utils tests passed!")
