import os
import shutil
import argparse
from pathlib import Path
from typing import Dict
from multiprocessing import Pool
import dace

import utils.config as config
from utils.reductions import add_all_reductions
from utils.unique_names import unique_names
from utils.benchmark_sdfg import instrument_sdfg
from utils.compile_if_propagated_sdfgs import compile_if_propagated_sdfgs
from utils.make_flattened_data_to_input import (
    make_flattened_data_to_non_transient_cpu_input,
    make_flattened_data_to_non_transient_gpu_input,
)
import os
import shutil

dace.config.Config.set("compiler", "cuda", "max_concurrent_streams", value="10")
dace.config.Config.set("compiler", "cuda", "default_block_size", value="256,1,1")
dace.config.Config.set("compiler", "default_data_types", value="C")

STARTER_SDFG_FILES = [
    "velocity_no_nproma_if_prop_lvn_only_0_istep_1.sdfgz",
    "velocity_no_nproma_if_prop_lvn_only_1_istep_1.sdfgz",
    "velocity_no_nproma_if_prop_lvn_only_1_istep_2.sdfgz",
    "velocity_no_nproma_if_prop_lvn_only_0_istep_2.sdfgz",
]

DEFAULT_CODEGEN_DIR = "codegen"


def sdfg_names(verify: bool = False):
    if verify:
        for f in STARTER_SDFG_FILES:
            assert Path(f).exists()
    return list(sorted(Path(f).stem for f in STARTER_SDFG_FILES))


def stage_input(name: str, stage: int, codegen_dir=DEFAULT_CODEGEN_DIR):
    if stage > 1:
        return f"{codegen_dir}/gpu_{name}_stage{stage - 1}.sdfgz"
    else:
        starter_map = {Path(f).stem: f for f in STARTER_SDFG_FILES}
        assert name in starter_map
        return starter_map[name]


def stage_output(name: str, stage: int, codegen_dir=DEFAULT_CODEGEN_DIR):
    if stage == 0:
        return stage_input(name, stage + 1, codegen_dir)
    else:
        return f"{codegen_dir}/gpu_{name}_stage{stage}.sdfgz"


def stage_inputs(stage: int, codegen_dir=DEFAULT_CODEGEN_DIR):
    return {name: stage_input(name, stage, codegen_dir) for name in sdfg_names()}


def stage_outputs(stage: int, codegen_dir=DEFAULT_CODEGEN_DIR):
    return {name: stage_output(name, stage, codegen_dir) for name in sdfg_names()}


def get_build_options():
    """Centralize environment variable parsing."""
    return {
        "release": os.getenv("_RELEASE", "0").lower() in ("1", "true", "yes"),
        "lowprec": os.getenv("_LOWPREC", "fp64").lower(),
        "build_for_integration": os.getenv("_BUILD_LIB_FOR_SOLVE_NH", "0").lower()
        in ("1", "true", "yes"),
        "tile": os.getenv("_TILE", "0").lower() in ("1", "true", "yes"),
        "profile": os.getenv("_PROFILE", "0").lower() in ("1", "true", "yes"),
        "reduce_bitwidth": os.getenv("_REDUCE_BITWIDTH_TRANSFORMATION", "0").lower()
        in ("1", "true", "yes"),
    }


def standard_main(stage_id, optimization_action_func, compile_extra_kwargs=None):
    """Standardized main loop for all stage scripts to reduce boilerplate."""
    import argparse

    argp = argparse.ArgumentParser()
    argp.add_argument(
        "--optimize", action=argparse.BooleanOptionalAction, default=False
    )
    argp.add_argument("--compile", action=argparse.BooleanOptionalAction, default=False)
    args = argp.parse_args()

    if not args.optimize and not args.compile:
        args.optimize, args.compile = True, True

    names = sdfg_names()

    if args.optimize:
        for name in names:
            infile = stage_input(name, stage_id)
            outfile = stage_output(name, stage_id)

            print(f"Stage #{stage_id}: Optimising {name} from {infile}")

            sdfg = dace.SDFG.from_file(infile)
            sdfg.name = name
            sdfg.validate()

            sdfg = optimization_action_func(sdfg)

            print(f"Stage #{stage_id}: Saved as {outfile}")
            sdfg.save(outfile, compress=True)

    if args.compile:
        sdfgs = {
            name: dace.SDFG.from_file(stage_output(name, stage_id)) for name in names
        }
        kwargs = compile_extra_kwargs or {}
        compile_action(stage_id, sdfgs, **kwargs)


def compile_action(
    stage: int,
    sdfgs: Dict[str, dace.SDFG],
    lib=False,
    allocation_names_to_comment_out=None,
    use_openacc_stream=False,
):
    dace.config.Config.set("compiler", "default_data_types", value="C")
    options = get_build_options()
    release = options["release"]

    for name, g in sdfgs.items():
        g.build_folder = f"{DEFAULT_CODEGEN_DIR}/stage{stage}/{name}"

    sdfg_list = list(sdfgs.values())
    unique_names(sdfg_list)

    if config.instrument:
        instrument_sdfg(sdfg_list)

    dace.Config.set("compiler", "cuda", "default_block_size", value="256,1,1")
    dace.Config.set("compiler", "cuda", "max_concurrent_streams", value="1")

    # Determine build configuration
    main_name = "main_gpu.cu" if stage >= 6 else "main.cu"
    is_lib = lib or (stage == 8 and options["build_for_integration"]) or stage == 9

    if stage == 1 and options["build_for_integration"]:
        for sdfg in sdfg_list:
            make_flattened_data_to_non_transient_cpu_input(sdfg)

    compile_if_propagated_sdfgs(
        sdfg_list,
        gpu=True,
        release=release,
        generate_code=True,
        lib=is_lib,
        main_name=None if is_lib else main_name,
        stage=stage,
        debuginfo=(stage < 8),
        allocation_names_to_comment_out=allocation_names_to_comment_out,
        use_openacc_stream=use_openacc_stream,
    )

    _finalize_binary(stage, release, options)


def _finalize_binary(stage, release, options):
    """Handles renaming and copying of the resulting binary or library."""
    opt_suffix = "_release" if release else "_debug"
    integration_suffix = (
        "_solve_nh_integration" if options["build_for_integration"] else "_standalone"
    )
    lowprec_suffix = f".{options['lowprec']}"

    target = (
        "libvelocity_gpu.so" if options["build_for_integration"] else "velocity_gpu"
    )
    if not Path(target).exists():
        return

    new_name = f"{target.split('.')[0]}_stage{stage}{integration_suffix}{opt_suffix}"
    if target.endswith(".so"):
        new_name += ".so"

    path = Path(target).rename(new_name)
    print(f"Output available: {path}")
    shutil.copy2(path, f"{path}{lowprec_suffix}")
