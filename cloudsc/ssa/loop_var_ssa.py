"""loop_var_ssa.py — SSA for loop iteration variables.

Gives each LoopRegion a unique iteration variable so that sibling loops
no longer share jk/jl/jm.  This unblocks LoopToMap which rejects loops
whose iteration variable is "used after the loop".

Usage (standalone):
    python -m ssa.loop_var_ssa cloudsc.sdfgz -o cloudsc_isolated.sdfgz
"""

from __future__ import annotations
import re

import dace
from tqdm import tqdm
from dace.sdfg.state import LoopRegion, ConditionalBlock


def _rename_loop_var(sdfg: dace.SDFG, loop: LoopRegion,
                     old_var: str, new_var: str):
    """Rename the iteration variable of a LoopRegion everywhere inside it."""
    from dace.properties import CodeBlock

    # 1. Update LoopRegion's own properties
    loop.loop_variable = new_var
    for attr in ('init_statement', 'update_statement', 'loop_condition'):
        val = getattr(loop, attr, None)
        if val is None:
            continue
        val_str = val.as_string if hasattr(val, 'as_string') else str(val)
        new_str = re.sub(r'\b' + re.escape(old_var) + r'\b', new_var, val_str)
        if new_str != val_str:
            setattr(loop, attr, CodeBlock(new_str))

    # 2. Replace in all states within the loop body (memlets, tasklets, maps)
    for state in loop.all_states():
        state.replace(old_var, new_var)

    # 3. Replace in interstate edges within the loop
    for region in loop.all_control_flow_regions():
        for e in region.edges():
            try:
                _, _, edge_data = e
            except (TypeError, ValueError):
                edge_data = e.data if hasattr(e, 'data') else e
            if hasattr(edge_data, 'assignments') and edge_data.assignments:
                new_assigns = {}
                for k, v in edge_data.assignments.items():
                    k_new = re.sub(r'\b' + re.escape(old_var) + r'\b',
                                   new_var, k)
                    v_new = re.sub(r'\b' + re.escape(old_var) + r'\b',
                                   new_var, str(v))
                    new_assigns[k_new] = v_new
                edge_data.assignments = new_assigns
            if hasattr(edge_data, 'condition'):
                cond_str = edge_data.condition.as_string
                new_str = re.sub(r'\b' + re.escape(old_var) + r'\b',
                                 new_var, cond_str)
                if new_str != cond_str:
                    edge_data.condition = CodeBlock(new_str)

    # 4. Replace in nested LoopRegion/ConditionalBlock expressions
    for region in loop.all_control_flow_regions():
        for child in region.nodes():
            if isinstance(child, LoopRegion) and child is not loop:
                if child.loop_variable == old_var:
                    child.loop_variable = new_var
                for attr in ('init_statement', 'update_statement',
                             'loop_condition'):
                    val = getattr(child, attr, None)
                    if val is None:
                        continue
                    val_str = (val.as_string if hasattr(val, 'as_string')
                               else str(val))
                    new_str = re.sub(r'\b' + re.escape(old_var) + r'\b',
                                     new_var, val_str)
                    if new_str != val_str:
                        setattr(child, attr, CodeBlock(new_str))
            elif isinstance(child, ConditionalBlock):
                for i, (cond, branch) in enumerate(child.branches):
                    if cond is not None:
                        cond_str = cond.as_string
                        new_str = re.sub(r'\b' + re.escape(old_var) + r'\b',
                                         new_var, cond_str)
                        if new_str != cond_str:
                            child.branches[i] = (CodeBlock(new_str), branch)

    # 5. Register the new symbol
    if new_var not in sdfg.symbols:
        old_type = sdfg.symbols.get(old_var, dace.int32)
        sdfg.add_symbol(new_var, old_type)


def isolate_loop_variables(sdfg: dace.SDFG) -> int:
    """Give each LoopRegion a unique iteration variable.

    Fortran-generated SDFGs reuse jk/jl/jm across sibling loops, which
    prevents LoopToMap because the variable appears "used after the loop".
    Returns number of loops renamed.
    """
    all_loops: list[LoopRegion] = []
    for cfg in sdfg.all_control_flow_regions():
        for child in cfg.nodes():
            if isinstance(child, LoopRegion):
                all_loops.append(child)

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
