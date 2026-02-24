import argparse
import dace
import utils.stages.common as common
from utils.int64_to_int32 import int64_to_int32
from utils.pre_gpu_fixes import make_arrays_persistent
from utils.reassign_vars import reassign_vars
from utils.change_reduction_schedule import change_reduction_schedule
from utils.tile import prune_unused_inputs_outputs
from utils.hacky_cfl_clipping_related_kernel_removal import (
    hacky_cfl_clipping_related_kernel_removal,
)
from utils.remove_unused_inconnectors_from_nestedsdfg import (
    remove_unused_inconnectors_from_nestedsdfg,
)
from utils.change_array_dtypes import change_array_dtypes

STAGE_ID = 7


def optimization_action(sdfg):
    """DEFINE THE OPTIMIZATION ACTION HERE"""
    make_arrays_persistent(sdfg)
    int64_to_int32(sdfg)
    reassign_vars(sdfg)
    sdfg.simplify()
    prune_unused_inputs_outputs(
        sdfg
    )  # NestedSDFG gets too many inputs/outputs no transformation exists to remove them
    sdfg.validate()
    change_reduction_schedule(sdfg)
    sdfg.validate()
    # HACK: Remove cfl clippign rleated kernels assuming it is 0. For now: Do not perform this semantics-changing transformation.
    # sdfg = hacky_cfl_clipping_related_kernel_removal(sdfg)
    sdfg.simplify()
    sdfg.validate()
    prune_unused_inputs_outputs(
        sdfg
    )  # NestedSDFG gets too many inputs/outputs no transformation exists to remove them
    remove_unused_inconnectors_from_nestedsdfg(sdfg)
    # Breaks the codegen
    change_array_dtypes(
        sdfg,
        array_names={
            "levmask",
            "levelmask",
            "cfl_clipping",
            "gpu_levmask",
            "gpu_levelmask",
            "gpu_cfl_clipping",
        },
        new_type=dace.uint8,
    )
    return sdfg


def main():
    common.standard_main(STAGE_ID, optimization_action)


if __name__ == "__main__":
    main()
