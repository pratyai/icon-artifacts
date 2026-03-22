"""loop_var_ssa.py — SSA for loop iteration variables.

Gives each LoopRegion a unique iteration variable so that sibling loops
no longer share jk/jl/jm.  This unblocks LoopToMap which rejects loops
whose iteration variable is "used after the loop".

Usage (standalone):
    python -m ssa.loop_var_ssa cloudsc.sdfgz -o cloudsc_isolated.sdfgz
"""

from __future__ import annotations

import dace
from tqdm import tqdm
from dace.sdfg.state import LoopRegion, ConditionalBlock

from ssa.graph_utils import (
    collect_all_loops, update_loop_metadata, update_conditional_metadata,
    update_interstate_edge,
)


def _rename_loop_var(sdfg: dace.SDFG, loop: LoopRegion,
                     old_var: str, new_var: str):
    """Rename the iteration variable of a LoopRegion everywhere inside it."""
    repl = {old_var: new_var}

    # 1. Update LoopRegion's own metadata (init, condition, update)
    update_loop_metadata(loop, repl)
    loop.loop_variable = new_var

    # 2. Replace in all states within the loop body (memlets, tasklets, maps)
    for state in loop.all_states():
        state.replace(old_var, new_var)

    # 3. Replace in interstate edges and nested block metadata
    for region in loop.all_control_flow_regions():
        for e in region.edges():
            if isinstance(e.data, dace.InterstateEdge):
                update_interstate_edge(e.data, repl)
        for child in region.nodes():
            if isinstance(child, LoopRegion) and child is not loop:
                update_loop_metadata(child, repl)
                if child.loop_variable == old_var:
                    child.loop_variable = new_var
            elif isinstance(child, ConditionalBlock):
                update_conditional_metadata(child, repl)

    # 4. Register the new symbol
    if new_var not in sdfg.symbols:
        old_type = sdfg.symbols.get(old_var, dace.int32)
        sdfg.add_symbol(new_var, old_type)


def isolate_loop_variables(sdfg: dace.SDFG) -> int:
    """Give each LoopRegion a unique iteration variable.

    Fortran-generated SDFGs reuse jk/jl/jm across sibling loops, which
    prevents LoopToMap because the variable appears "used after the loop".
    Returns number of loops renamed.
    """
    all_loops = collect_all_loops(sdfg)

    counter: dict[str, int] = {}
    for loop in tqdm(all_loops, desc="Isolating loop vars", unit="loop"):
        var = loop.loop_variable
        idx = counter.get(var, 0)
        counter[var] = idx + 1
        new_var = f"{var}__l{idx}"
        _rename_loop_var(sdfg, loop, var, new_var)

    print(f"Loop variable isolation: renamed {len(all_loops)} loop variables")
    return len(all_loops)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="SSA for loop iteration variables")
    parser.add_argument("input", type=str, help="Input SDFG file")
    parser.add_argument("-o", "--output", type=str, default=None)
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.input)
    isolate_loop_variables(sdfg)

    out_path = args.output or args.input.replace(".sdfgz", "_loopssa.sdfgz")
    sdfg.save(out_path)
    print(f"Saved to {out_path}")
