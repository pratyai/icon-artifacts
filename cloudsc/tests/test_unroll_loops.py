
import dace
import pytest
from ssa.unroll import unroll_loops

def test_unroll_loops_basic():
    sdfg = dace.SDFG("test_unroll")
    sdfg.add_symbol("N", dace.int32)
    
    # Loop with extent N (0 to 4, step 1)
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
    
    # Target N=5
    symbol_map = {"N": 5}
    unroll_loops(sdfg, symbol_map)
    
    # Assertions
    # 1. The loop should be gone
    assert not any(isinstance(n, dace.sdfg.state.LoopRegion) for n in sdfg.nodes())
    # 2. There should be 5 states (or more depending on how unroll is implemented)
    # The current _manual_loop_unroll creates one state per iteration.
    states = list(sdfg.all_states())
    assert len(states) >= 5
    
    # 3. Check labels or content to ensure i was replaced
    labels = [s.label for s in states]
    assert any("_i_0" in l for l in labels)
    assert any("_i_4" in l for l in labels)
    
    sdfg.validate()

def test_unroll_loops_no_match():
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
    
    # Target M=10 (doesn't match N)
    symbol_map = {"M": 10}
    unroll_loops(sdfg, symbol_map)
    
    # Loop should STILL BE THERE
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
    
    # Unroll inner loop (N=3)
    unroll_loops(sdfg, {"N": 3})
    
    # Outer loop should exist, inner loop should be unrolled INSIDE outer
    assert any(n.label == "outer" for n in sdfg.nodes())
    assert not any(n.label == "inner" for n in outer.nodes())
    
    # Check that inner_state was copied multiple times inside outer
    inner_states = [s for s in outer.all_states() if "inner_state" in s.label]
    assert len(inner_states) == 3
    
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
    
    # To unroll, the resulting iteration count (5) must be in potential_ranges
    unroll_loops(sdfg, {"START": 10, "COUNT": 5})
    
    states = list(sdfg.all_states())
    # Should have 5 iterations
    body_states = [s for s in states if "body" in s.label]
    assert len(body_states) == 5
    
    # Check values in tasklets
    codes = sorted([s.nodes()[0].code.as_string for s in body_states])
    assert codes == ["x = 10", "x = 2", "x = 4", "x = 6", "x = 8"]
    
    sdfg.validate()

def test_unroll_loops_multi_round():
    # Outer loop (2 iterations) contains inner loop (3 iterations)
    # Both extents are symbols
    sdfg = dace.SDFG("multiround")
    sdfg.add_symbol("N", dace.int32)
    sdfg.add_symbol("M", dace.int32)
    
    outer = dace.sdfg.state.LoopRegion("outer", "j < N", "j", "j = 0", "j = j + 1")
    sdfg.add_node(outer, is_start_block=True)
    
    inner = dace.sdfg.state.LoopRegion("inner", "i < M", "i", "i = 0", "i = i + 1")
    outer.add_node(inner, is_start_block=True)
    inner.add_state("body").add_tasklet("t", {}, {}, "x = i + j")
    
    # Unroll both
    unroll_loops(sdfg, {"N": 2, "M": 3})
    
    assert not any(isinstance(n, dace.sdfg.state.LoopRegion) for n in sdfg.all_nodes_recursive())
    
    # Total body states should be 2 * 3 = 6
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

    unroll_loops(sdfg, {"N": 3})
    
    found_conditions = []
    for edge in sdfg.edges():
        if isinstance(edge.data, dace.InterstateEdge) and not edge.data.is_unconditional():
            found_conditions.append(edge.data.condition.as_string)
    
    import re
    # Normalize by removing spaces and parentheses for comparison
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
    
    # Create a nested SDFG that uses 'i' in its symbol mapping
    nsdfg = dace.SDFG("nested")
    nsdfg.add_symbol("M", dace.int32)
    nstate = nsdfg.add_state("nstate")
    nstate.add_tasklet("nt", {}, {}, "y = M")
    
    # 'M' in nested SDFG is mapped to 'i + 1' in parent
    ls.add_nested_sdfg(nsdfg, {}, {}, {"M": "i + 1"})
    
    unroll_loops(sdfg, {"N": 2})
    
    # Check that M was replaced in each iteration
    # Iteration 0: i=0 -> M = 1
    # Iteration 1: i=1 -> M = 2

    nested_nodes = [n for s in sdfg.all_states() for n in s.nodes() if isinstance(n, dace.sdfg.nodes.NestedSDFG)]
    assert len(nested_nodes) == 2

    mappings = [str(n.symbol_mapping["M"]) for n in nested_nodes]

    assert "1" in mappings
    assert "2" in mappings

    sdfg.validate()

if __name__ == "__main__":
    test_unroll_loops_basic()
    test_unroll_loops_no_match()
    test_unroll_loops_nested()
    test_unroll_loops_complex_range()
    test_unroll_loops_multi_round()
    test_unroll_loops_with_interstate_condition()
    test_unroll_loops_nested_sdfg_symbol_mapping()
    print("All tests passed!")
