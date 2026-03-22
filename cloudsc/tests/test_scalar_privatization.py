
import dace
import pytest
from ssa.scalar_privatization import (
    privatize_scalars, _collect_refs, build_plan,
    _is_private,
)
from ssa.graph_utils import loop_interior, written_transient_scalars

def test_privatize_sequential_loops():
    sdfg = dace.SDFG("test_priv")
    sdfg.add_scalar("s1", dace.float64, transient=True)
    sdfg.add_scalar("s2", dace.float64, transient=True)

    # Loop 1: writes s1
    l1 = dace.sdfg.state.LoopRegion("l1", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(l1)
    st1 = l1.add_state("st1")
    st1.add_edge(st1.add_tasklet("t1", {}, {"o"}, "o=1"), "o", st1.add_write("s1"), None, dace.Memlet("s1"))

    # Loop 2: writes s2
    l2 = dace.sdfg.state.LoopRegion("l2", "j < 10", "j", "j=0", "j=j+1")
    sdfg.add_node(l2)
    sdfg.add_edge(l1, l2, dace.InterstateEdge())
    st2 = l2.add_state("st2")
    st2.add_edge(st2.add_tasklet("t2", {}, {"o"}, "o=2"), "o", st2.add_write("s2"), None, dace.Memlet("s2"))

    count = privatize_scalars(sdfg)
    assert count == 2

    # Check that AccessNodes use privatized names
    an1 = [n for n in st1.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    an2 = [n for n in st2.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    assert an1.data == "s1__p1"
    assert an2.data == "s2__p1"

def test_privatize_shared_scalar():
    sdfg = dace.SDFG("test_shared")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    l1 = dace.sdfg.state.LoopRegion("l1", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(l1)
    l1.add_state("s1").add_write("s")
    
    l2 = dace.sdfg.state.LoopRegion("l2", "j < 10", "j", "j=0", "j=j+1")
    sdfg.add_node(l2)
    sdfg.add_edge(l1, l2, dace.InterstateEdge())
    l2.add_state("s2").add_read("s")
    
    # 's' is written in l1 and read in l2 -> NOT private to either
    count = privatize_scalars(sdfg)
    assert count == 0

def test_privatize_not_private():
    sdfg = dace.SDFG("test_not_priv")
    sdfg.add_scalar("s", dace.float64, transient=True)
    
    l1 = dace.sdfg.state.LoopRegion("l1", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(l1)
    s1 = l1.add_state("s1")
    s1.add_write("s")
    
    # Read outside
    after = sdfg.add_state("after")
    sdfg.add_edge(l1, after, dace.InterstateEdge())
    after.add_read("s")
    
    count = privatize_scalars(sdfg)
    assert count == 0
    
    an1 = [n for n in s1.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    assert an1.data == "s"

def test_collect_refs():
    sdfg = dace.SDFG("test_refs")
    sdfg.add_scalar("s", dace.float64, transient=True)

    s1 = sdfg.add_state("s1")
    s1.add_edge(s1.add_tasklet("t", {}, {"o"}, "o=1"), "o", s1.add_write("s"), None, dace.Memlet("s"))

    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge(condition="s > 0"))

    refs = _collect_refs(sdfg)
    assert s1 in refs["s"]
    # Interstate edge condition references s
    assert any(isinstance(site, dace.InterstateEdge) for site in refs["s"])


def test_is_private():
    sdfg = dace.SDFG("test_private")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    ls = loop.add_state("ls")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", ls.add_write("s"), None, dace.Memlet("s"))

    refs = _collect_refs(sdfg)
    loop_states, loop_blocks, loop_edges = loop_interior(loop)
    assert _is_private(refs, "s", loop_states, loop_blocks, loop_edges)

    # Add external read -> no longer private
    after = sdfg.add_state("after")
    sdfg.add_edge(loop, after, dace.InterstateEdge())
    after.add_read("s")

    refs = _collect_refs(sdfg)
    assert not _is_private(refs, "s", loop_states, loop_blocks, loop_edges)


def test_written_transient_scalars():
    sdfg = dace.SDFG("test_written")
    sdfg.add_scalar("s", dace.float64, transient=True)
    sdfg.add_scalar("r", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    ls = loop.add_state("ls")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", ls.add_write("s"), None, dace.Memlet("s"))
    ls.add_read("r")  # read-only, not written

    written = written_transient_scalars(sdfg, loop.all_states())
    assert "s" in written
    assert "r" not in written


def test_build_plan_inner_first():
    """Inner loop privatization should allow outer loop to see the scalar as private."""
    sdfg = dace.SDFG("test_inner_first")
    sdfg.add_scalar("s", dace.float64, transient=True)

    outer = dace.sdfg.state.LoopRegion("outer", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(outer)
    inner = dace.sdfg.state.LoopRegion("inner", "j < 5", "j", "j=0", "j=j+1")
    outer.add_node(inner, is_start_block=True)
    ls = inner.add_state("ls")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", ls.add_write("s"), None, dace.Memlet("s"))

    plan = build_plan(sdfg)
    # Inner loop should privatize s
    assert len(plan) >= 1
    inner_repl = plan[0][1]
    assert "s" in inner_repl


def test_build_plan_metadata_ref():
    """Scalar referenced in loop metadata should NOT be private to that loop."""
    sdfg = dace.SDFG("test_meta")
    sdfg.add_scalar("s", dace.float64, transient=True)

    # Write s before the loop
    init = sdfg.add_state("init")
    init.add_edge(init.add_tasklet("t", {}, {"o"}, "o=1"), "o", init.add_write("s"), None, dace.Memlet("s"))

    loop = dace.sdfg.state.LoopRegion("l", "i < s", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    sdfg.add_edge(init, loop, dace.InterstateEdge())
    ls = loop.add_state("ls")
    ls.add_edge(ls.add_tasklet("t2", {}, {"o"}, "o=2"), "o", ls.add_write("s"), None, dace.Memlet("s"))

    plan = build_plan(sdfg)
    # s is referenced in the loop condition AND written before -> not private
    assert all("s" not in repl for _, repl in plan)


def test_build_plan_interstate_edge_ref():
    """Scalar referenced in interstate edge outside loop should NOT be private."""
    sdfg = dace.SDFG("test_edge_ref")
    sdfg.add_scalar("s", dace.float64, transient=True)

    loop = dace.sdfg.state.LoopRegion("l", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(loop)
    ls = loop.add_state("ls")
    ls.add_edge(ls.add_tasklet("t", {}, {"o"}, "o=1"), "o", ls.add_write("s"), None, dace.Memlet("s"))

    after = sdfg.add_state("after")
    sdfg.add_edge(loop, after, dace.InterstateEdge(assignments={"x": "s + 1"}))

    plan = build_plan(sdfg)
    assert all("s" not in repl for _, repl in plan)


def test_privatize_not_skipped_when_written_outside():
    """Scalar written both inside a loop and outside should NOT be privatized
    (caught by _is_private, not the write_counts check)."""
    sdfg = dace.SDFG("test_outside_write")
    sdfg.add_scalar("s", dace.float64, transient=True)

    # Write s outside any loop
    init = sdfg.add_state("init", is_start_block=True)
    init.add_edge(init.add_tasklet("t0", {}, {"o"}, "o=0"), "o",
                  init.add_write("s"), None, dace.Memlet("s"))

    # Loop also writes s
    l1 = dace.sdfg.state.LoopRegion("l1", "i < 10", "i", "i=0", "i=i+1")
    sdfg.add_node(l1)
    sdfg.add_edge(init, l1, dace.InterstateEdge())
    st1 = l1.add_state("st1")
    st1.add_edge(st1.add_tasklet("t1", {}, {"o"}, "o=1"), "o",
                 st1.add_write("s"), None, dace.Memlet("s"))

    count = privatize_scalars(sdfg)
    assert count == 0

    # Name unchanged — not private because of the outside write
    an1 = [n for n in st1.nodes() if isinstance(n, dace.nodes.AccessNode)][0]
    assert an1.data == "s"


if __name__ == "__main__":
    test_privatize_sequential_loops()
    test_privatize_not_skipped_when_written_outside()
    test_privatize_shared_scalar()
    test_privatize_not_private()
    test_collect_refs()
    test_is_private()
    test_written_transient_scalars()
    test_build_plan_inner_first()
    test_build_plan_metadata_ref()
    test_build_plan_interstate_edge_ref()
    print("Scalar privatization tests passed!")
