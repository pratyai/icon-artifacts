import dace
from dace.transformation.interstate import LoopToMap
from dace.transformation.dataflow import MapCollapse
from utils.count import count_symbols_use_defs
import utils.stages.common as common
import utils.config as config
from utils.unique_names import unique_names
from utils.benchmark_sdfg import instrument_sdfg
from utils.compile_if_propagated_sdfgs import compile_if_propagated_sdfgs
import argparse

STAGE_ID = 2


def optimization_action(sdfg):
    """DEFINE THE OPTIMIZATION ACTION HERE"""
    sdfg.apply_transformations_repeated(LoopToMap, permissive=True)
    count_symbols_use_defs(sdfg, verbose=config.verbose, use_assert=True)
    sdfg.simplify(skip=["ArrayElimination", "InlineSDFG"])
    sdfg.apply_transformations_repeated(MapCollapse)
    sdfg.simplify(skip=["ArrayElimination", "InlineSDFG"])
    return sdfg


def main():
    common.standard_main(STAGE_ID, optimization_action)


if __name__ == "__main__":
    main()
