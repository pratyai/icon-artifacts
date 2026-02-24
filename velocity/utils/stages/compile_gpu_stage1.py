import dace
from dace.transformation.interstate import ContinueToCondition
from dace.transformation.passes import (
    StructToContainerGroups,
    SymbolPropagation,
    ConstantPropagation,
)
from utils.clean_bad_views import clean_bad_views
import utils.stages.common as common
import utils.config as config
from utils.reductions import add_all_reductions
from utils.unique_names import unique_names
from utils.benchmark_sdfg import instrument_sdfg
from utils.compile_if_propagated_sdfgs import compile_if_propagated_sdfgs
import argparse
from utils.init_transient_zero import init_transient_zero
from utils.reassign_vars import reassign_vars

STAGE_ID = 1
from utils.prune_names import prune_names


def optimization_action(sdfg):
    """DEFINE THE OPTIMIZATION ACTION HERE"""
    # Need to remove partial view towers (it is illegal and should not happen, but it happens)
    clean_bad_views(sdfg)
    sdfg.apply_transformations_repeated(ContinueToCondition)

    # Flattening needs to run before everything
    StructToContainerGroups(
        validate=False,
        save_steps=False,
        verbose=config.verbose,
        simplify=False,
        interface_with_struct_copy=True,
        interface_to_gpu=False,
        clean_trivial_views=True,
        shallow_copy=False,
        shallow_copy_to_gpu=False,
        taskloop=False,
    ).apply_pass(sdfg, {})

    sdfg.simplify(skip=["ArrayElimination"])
    SymbolPropagation().apply_pass(sdfg, {})
    sdfg.simplify(skip=["ArrayElimination"])

    if config.reduction:
        add_all_reductions(sdfg)

    ConstantPropagation().apply_pass(sdfg, {})
    sdfg = prune_names(sdfg)
    return sdfg


def main():
    common.standard_main(STAGE_ID, optimization_action)


if __name__ == "__main__":
    main()
