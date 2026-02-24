import dace
from dace.transformation.interstate import ConditionFusion
from dace.transformation.passes import InlineSDFGs
from dace.transformation.dataflow import MapCollapse, MapFusion
import utils.stages.common as common
import utils.config as config
from utils.unique_names import unique_names
from utils.benchmark_sdfg import instrument_sdfg
from utils.compile_if_propagated_sdfgs import compile_if_propagated_sdfgs
from utils.map_state_fission import MapStateFission
from utils.prune_unused_inputs_outputs import (
    prune_unused_inputs_outputs,
    prune_unused_inputs_outputs_recursive,
)
from utils.preprocess_tough_nut import preprocess_tough_nut
import argparse

STAGE_ID = 3


def optimization_action(sdfg):
    """DEFINE THE OPTIMIZATION ACTION HERE"""
    sdfg.apply_transformations_repeated(MapStateFission, {"allow_transients": True})

    prune_unused_inputs_outputs(
        sdfg
    )  # NestedSDFG gets too many inputs/outputs no transformation exists to remove them
    prune_unused_inputs_outputs_recursive(sdfg)
    sdfg.validate()

    sdfg.apply_transformations_repeated(ConditionFusion)
    prune_unused_inputs_outputs(sdfg)
    InlineSDFGs().apply_pass(sdfg, {})

    k = sdfg.apply_transformations_repeated(MapCollapse)
    if config.verbose:
        print(f"Applied MapCollapse {k} time(s)")
    k = sdfg.apply_transformations_repeated(MapFusion)
    for n, g in sdfg.all_nodes_recursive():
        if isinstance(n, dace.nodes.NestedSDFG):
            if isinstance(n, dace.nodes.NestedSDFG):
                k = n.sdfg.apply_transformations_repeated(MapFusion)
                if config.verbose:
                    print(f"Applied MapFusion {k} time(s) to NestedSDFG {n.sdfg.name}")
    if config.verbose:
        print(f"Applied MapFusion {k} time(s)")
    k = sdfg.apply_transformations_repeated(MapCollapse)
    if config.verbose:
        print(f"Applied MapCollapse {k} time(s)")

    sdfg.simplify(skip=["StateFusion"])
    prune_unused_inputs_outputs(sdfg)
    InlineSDFGs().apply_pass(sdfg, {})
    k = sdfg.apply_transformations_repeated(MapCollapse)
    if config.verbose:
        print(f"Applied MapCollapse {k} time(s)")

    prune_unused_inputs_outputs(
        sdfg
    )  # NestedSDFG gets too many inputs/outputs no transformation exists to remove them
    prune_unused_inputs_outputs_recursive(sdfg)
    sdfg.validate()

    return sdfg


def main():
    common.standard_main(STAGE_ID, optimization_action)


if __name__ == "__main__":
    main()
