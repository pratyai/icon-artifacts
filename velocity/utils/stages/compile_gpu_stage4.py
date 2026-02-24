import dace
from dace.transformation.passes import InlineSDFGs
from dace.transformation.dataflow import MapCollapse, MapFusion
import utils.stages.common as common
import utils.config as config
from utils.unique_names import unique_names
from utils.benchmark_sdfg import instrument_sdfg
from utils.compile_if_propagated_sdfgs import compile_if_propagated_sdfgs
from utils.map_state_fission import MapStateFission
from utils.prune_unused_inputs_outputs import prune_unused_inputs_outputs
import argparse
from utils.propagate_if_cond import propagate_if_cond
from utils.demote_symbol_to_scalar import demote_symbol_to_scalar

STAGE_ID = 4


def optimization_action(sdfg):
    """DEFINE THE OPTIMIZATION ACTION HERE"""
    sdfg.validate()
    sdfg.simplify(skip=["StateFusion"])
    sdfg.validate()

    prune_unused_inputs_outputs(sdfg)

    InlineSDFGs().apply_pass(sdfg, {})
    k = sdfg.apply_transformations_repeated(MapCollapse, permissive=True)
    if config.verbose:
        print(f"Applied MapCollapse {k} time(s)")
    k = sdfg.apply_transformations_repeated(MapFusion)
    for n, g in sdfg.all_nodes_recursive():
        if isinstance(n, dace.nodes.NestedSDFG):
            if isinstance(n, dace.nodes.NestedSDFG):
                k = n.sdfg.apply_transformations_repeated(MapFusion, permissive=True)
                if config.verbose:
                    print(f"Applied MapFusion {k} time(s) to NestedSDFG {n.sdfg.name}")

    if config.verbose:
        print(f"Applied MapFusion {k} time(s)")
    k = sdfg.apply_transformations_repeated(MapCollapse, permissive=True)
    if config.verbose:
        print(f"Applied MapCollapse {k} time(s)")

    sdfg.simplify(skip=["StateFusion"])
    prune_unused_inputs_outputs(sdfg)
    InlineSDFGs().apply_pass(sdfg, {})
    k = sdfg.apply_transformations_repeated(MapCollapse, permissive=True)
    if config.verbose:
        print(f"Applied MapCollapse {k} time(s)")

    sdfg.simplify(skip=["StateFusion"])
    # I saw trurthy ifs, propagate those conditions and try to fuse states agian
    # TODO: Crashes sometimes
    propagate_if_cond(sdfg, sdfg, None, None, True)
    # Prevents some transformations from being applied
    # This is not a symbol anymore (? - it was before why)
    # TODO: Check if this is necessary, if so fix
    assert "tmp_call_18" in sdfg.symbols
    if "tmp_call_18" in sdfg.symbols:
        demote_symbol_to_scalar(sdfg, "tmp_call_18")

    sdfg.validate()
    sdfg.simplify(skip=["StateFusion"])
    sdfg.validate()
    return sdfg


def main():
    common.standard_main(STAGE_ID, optimization_action)


if __name__ == "__main__":
    main()
