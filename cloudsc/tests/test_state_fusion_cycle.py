import dace
import pytest
import networkx as nx
from ssa.state_fusion import fuse_all_states

def test_state_fusion_cycle_repro():
    """
    S1 has two paths for 'x' that are already ordered by 'y'.
    Path 1: ... -> x_sink1
    Path 2: x_sink1 -> ... -> y -> ... -> x_sink2
    
    If we unify x_sink1 and x_sink2 in S1, we might create a cycle 
    if we merge the 'later' one into the 'earlier' one.
    """
    sdfg = dace.SDFG("cycle_repro")
    sdfg.add_scalar("x", dace.float64, transient=True)
    sdfg.add_scalar("y", dace.float64, transient=True)
    
    s1 = sdfg.add_state("s1")
    s2 = sdfg.add_state("s2")
    sdfg.add_edge(s1, s2, dace.InterstateEdge())
    
    # S1 Construction
    # Path 1 ends in x_snk1
    t1 = s1.add_tasklet("t1", {}, {"out"}, "out = 1.0")
    x_snk1 = s1.add_write("x")
    s1.add_edge(t1, "out", x_snk1, None, dace.Memlet("x"))
    
    # Path 2 starts from x_snk1 (e.g. a read) and ends in x_snk2
    # This makes x_snk1 NOT a sink anymore. 
    # To have two sinks, they must be truly parallel or disjoint.
    
    # Let's try: Two parallel AccessNodes for x, but one tasklet 
    # depends on the other path.
    
    # Path A: Tasklet A -> x_snk1
    # Path B: Tasklet B -> x_snk2
    # Dependency: x_snk1 -> Tasklet C -> y -> Tasklet B
    
    tA = s1.add_tasklet("tA", {}, {"o"}, "o=1")
    xa = s1.add_access("x")
    s1.add_edge(tA, "o", xa, None, dace.Memlet("x"))
    
    tC = s1.add_tasklet("tC", {"i"}, {"o"}, "o=i")
    ya = s1.add_access("y")
    s1.add_edge(xa, None, tC, "i", dace.Memlet("x"))
    s1.add_edge(tC, "o", ya, None, dace.Memlet("y"))
    
    tB = s1.add_tasklet("tB", {"i"}, {"o"}, "o=i")
    xb = s1.add_access("x")
    s1.add_edge(ya, None, tB, "i", dace.Memlet("y"))
    s1.add_edge(tB, "o", xb, None, dace.Memlet("x"))
    
    # In S1: xa and xb are both AccessNodes for 'x'.
    # xa is NOT a sink (it has out-edges).
    # xb IS a sink.
    
    # S2: Read x
    s2_read = s2.add_read("x")
    t2 = s2.add_tasklet("t2", {"i"}, {}, "print(i)")
    s2.add_edge(s2_read, None, t2, "i", dace.Memlet("x"))
    
    # Current logic:
    # S1 sinks for x: [xb]
    # S2 sources for x: [s2_read]
    # Merge s2_read into xb. Safe.
    
    # What if we had a NestedSDFG that makes xa look like a sink?
    # Or if they are in different Scopes?
    
    fuse_all_states(sdfg)
    sdfg.validate()

if __name__ == "__main__":
    test_state_fusion_cycle_repro()
    print("Test passed!")
