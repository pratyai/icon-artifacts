import os
import dace
import argparse
import utils.stages.common as common
from utils.change_flatten_lib_to_shallow_copy import change_flatten_lib_to_shallow_copy
from utils.input_to_gpu import input_to_gpu
from utils.add_set_zero import add_set_zero
from utils.make_flattened_data_to_input import (
    make_flattened_data_to_non_transient_cpu_input,
    make_flattened_data_to_non_transient_gpu_input,
)

from utils.profiling_patches import (
    remove_profiling_states,
    remove_sync_states,
    insert_program_entry_exit_syncs,
    rm_redundant_copies,
)

STAGE_ID = 9

_allocation_names_to_comment_out = set()


def optimization_action(sdfg):
    global _allocation_names_to_comment_out
    """ DEFINE THE OPTIMIZATION ACTION HERE """
    # Prepares SDFG to be built as a GPU library
    shallow_copy_used_structs = ["p_prog", "p_int", "p_metrics", "p_patch", "p_diag"]
    deflatten_used_structs = ["p_diag"]
    _allocation_names_to_comment_out = change_flatten_lib_to_shallow_copy(
        sdfg, shallow_copy_used_structs, deflatten_used_structs
    )

    sdfg.validate()
    input_to_gpu(sdfg, "z_w_concorr_me")
    input_to_gpu(sdfg, "z_kin_hor_e")
    input_to_gpu(sdfg, "z_vt_ie")
    sdfg.validate()

    make_flattened_data_to_non_transient_gpu_input(sdfg)
    sdfg.validate()

    remove_profiling_states(sdfg)
    remove_sync_states(sdfg)
    insert_program_entry_exit_syncs(sdfg)
    rm_redundant_copies(sdfg)

    return sdfg


def main():
    common.standard_main(
        STAGE_ID,
        optimization_action,
        compile_extra_kwargs={
            "allocation_names_to_comment_out": _allocation_names_to_comment_out
        },
    )


if __name__ == "__main__":
    main()
