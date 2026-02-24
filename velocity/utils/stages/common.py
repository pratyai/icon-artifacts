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

dace.config.Config.set("compiler", "cuda", "max_concurrent_streams", value="10")
dace.config.Config.set("compiler", "default_data_types", value="C")

STARTER_SDFG_FILES = [
    "velocity_no_nproma_if_prop_lvn_only_0_istep_1.sdfgz",
    "velocity_no_nproma_if_prop_lvn_only_1_istep_1.sdfgz",
    "velocity_no_nproma_if_prop_lvn_only_1_istep_2.sdfgz",
    "velocity_no_nproma_if_prop_lvn_only_0_istep_2.sdfgz",
]

DEFAULT_CODEGEN_DIR = 'codegen'

def sdfg_names(verify:bool=False):
  if verify:
    for f in STARTER_SDFG_FILES:
      assert Path(f).exists()
  return list(sorted(Path(f).stem for f in STARTER_SDFG_FILES))

def stage_input(name:str, stage:int, codegen_dir=DEFAULT_CODEGEN_DIR):
  if stage > 1:
    return f"{codegen_dir}/gpu_{name}_stage{stage - 1}.sdfgz"
  else:
    starter_map = {Path(f).stem : f for f in STARTER_SDFG_FILES}
    assert name in starter_map
    return starter_map[name]

def stage_output(name:str, stage:int, codegen_dir=DEFAULT_CODEGEN_DIR):
  if stage == 0:
    return stage_input(name, stage+1, codegen_dir)
  else:
    return f"{codegen_dir}/gpu_{name}_stage{stage}.sdfgz"

def stage_inputs(stage: int, codegen_dir=DEFAULT_CODEGEN_DIR):
  return {name: stage_input(name, stage, codegen_dir) for name in sdfg_names()}

def stage_outputs(stage: int, codegen_dir=DEFAULT_CODEGEN_DIR):
  return {name: stage_output(name, stage, codegen_dir) for name in sdfg_names()}

def compile_action(stage: int, sdfgs: Dict[str, dace.SDFG], lib,
                    allocation_names_to_comment_out: set,
                    use_openacc_stream: bool):
  dace.config.Config.set('compiler', 'default_data_types', value='C')
  release = os.getenv('_RELEASE', '0').lower() in ('1', 'true', 'yes')
  for name, g in sdfgs.items():
      g.build_folder = f"{common.DEFAULT_CODEGEN_DIR}/stage{stage}/{name}"
  sdfgs = list(sdfgs.values())
  # Avoid name conflicts.
  unique_names(sdfgs)
  # Add instrumentation if necessary.
  if config.instrument:
      instrument_sdfg(sdfgs)

  dace.Config.set('compiler', 'cuda', 'default_block_size', value="256,1,1")
  dace.Config.set('compiler', 'cuda', 'max_concurrent_streams', value="1")
  _build_for_integration = os.getenv('_BUILD_LIB_FOR_SOLVE_NH', '0').lower() in ('1', 'true', 'yes')

    print(f"Stage #{stage_id}: Optimising {name} from {infile}")

    sdfg = dace.SDFG.from_file(infile)
    sdfg.name = name
    sdfg.validate()

    sdfg = func(sdfg)

    print(f"Stage #{stage_id}: Saved as {outfile}")
    sdfg.save(outfile, compress=True)
    return True


def _optimize_single(args):
    """Worker function for parallel optimization."""
    name, stage_id, func = args
    infile = stage_input(name, stage_id)
    outfile = stage_output(name, stage_id)

    print(f"Stage #{stage_id}: Optimising {name} from {infile}")

    sdfg = dace.SDFG.from_file(infile)
    sdfg.name = name
    sdfg.validate()

    result = func(sdfg)
    if isinstance(result, tuple):
        sdfg, metadata = result
    else:
        sdfg, metadata = result, None

    print(f"Stage #{stage_id}: Saved as {outfile}")
    sdfg.save(outfile, compress=True)
    return True, metadata


def get_build_options(args=None):
    """Centralize options parsing (args override env vars)."""
    options = {
        "release": os.getenv("_RELEASE", "0").lower() in ("1", "true", "yes"),
        "lowprec": os.getenv("_LOWPREC", "fp64").lower(),
        "build_for_integration": os.getenv("_BUILD_LIB_FOR_SOLVE_NH", "0").lower()
        in ("1", "true", "yes"),
        "tile": os.getenv("_TILE", "0").lower() in ("1", "true", "yes"),
        "profile": os.getenv("_PROFILE", "0").lower() in ("1", "true", "yes"),
        "reduce_bitwidth": os.getenv("_REDUCE_BITWIDTH_TRANSFORMATION", "0").lower()
        in ("1", "true", "yes"),
        "lower_all": os.getenv("_LOWER_ALL", "0").lower() in ("1", "true", "yes"),
    }

    if args:
        if args.release is not None:
            options["release"] = args.release
        if args.lowprec is not None:
            options["lowprec"] = args.lowprec.lower()
        if args.integration is not None:
            options["build_for_integration"] = args.integration
        if args.tile is not None:
            options["tile"] = args.tile
        if args.profile is not None:
            options["profile"] = args.profile
        if args.reduce_bitwidth is not None:
            options["reduce_bitwidth"] = args.reduce_bitwidth

    # Write back to environment for any child processes or DaCe passes that check them directly
    os.environ["_RELEASE"] = "1" if options["release"] else "0"
    os.environ["_LOWPREC"] = options["lowprec"]
    os.environ["_BUILD_LIB_FOR_SOLVE_NH"] = (
        "1" if options["build_for_integration"] else "0"
    )
    os.environ["_TILE"] = "1" if options["tile"] else "0"
    os.environ["_PROFILE"] = "1" if options["profile"] else "0"
    os.environ["_REDUCE_BITWIDTH_TRANSFORMATION"] = (
        "1" if options["reduce_bitwidth"] else "0"
    )

    return options


def standard_main(stage_id, optimization_action_func, compile_extra_kwargs=None):
    """Standardized main loop for all stage scripts to reduce boilerplate."""
    argp = argparse.ArgumentParser()
    argp.add_argument("--optimize", action=argparse.BooleanOptionalAction, default=None)
    argp.add_argument("--compile", action=argparse.BooleanOptionalAction, default=None)

    # Optional overrides for environment variables
    argp.add_argument("--release", action=argparse.BooleanOptionalAction, default=None)
    argp.add_argument(
        "--lowprec", type=str, default=None, choices=["fp64", "fp32", "f32", "f64"]
    )
    argp.add_argument(
        "--integration", action=argparse.BooleanOptionalAction, default=None
    )
    argp.add_argument("--tile", action=argparse.BooleanOptionalAction, default=None)
    argp.add_argument("--profile", action=argparse.BooleanOptionalAction, default=None)
    argp.add_argument(
        "--reduce-bitwidth", action=argparse.BooleanOptionalAction, default=None
    )

    args = argp.parse_args()

    # Default to both if neither is specified
    if args.optimize is None and args.compile is None:
        args.optimize, args.compile = True, True

    # Initialize environment
    get_build_options(args)
    names = sdfg_names()

    all_metadata = []
    if args.optimize:
        tasks = [(name, stage_id, optimization_action_func) for name in names]

        # Disable OpenMP thread pooling inside DaCe during multiprocessing to avoid oversubscription
        dace.config.Config.set("compiler", "num_threads", value="1")

        single_threaded = os.getenv("SINGLE_THREADED", "0").lower() in (
            "1",
            "true",
            "yes",
        )
        if single_threaded:
            results = [_optimize_single(t) for t in tasks]
        else:
            with Pool(processes=min(len(tasks), os.cpu_count())) as pool:
                results = pool.map(_optimize_single, tasks)

        all_metadata = [r[1] for r in results if r[1] is not None]

    if args.compile:
        sdfgs = {
            name: dace.SDFG.from_file(stage_output(name, stage_id)) for name in names
        }
        kwargs = compile_extra_kwargs or {}
        compile_action(stage_id, sdfgs, **kwargs)

    return all_metadata


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
        if options["permute_dimensions"]:
            from utils.reshape_kernels import update_gpu_block_size

            update_gpu_block_size(g, [32, 32, 1])

    sdfg_list = list(sdfgs.values())
    unique_names(sdfg_list)

    if config.instrument:
        instrument_sdfg(sdfg_list)

    block_size_str = "32,32,1" if options["permute_dimensions"] else "256,1,1"
    dace.Config.set("compiler", "cuda", "default_block_size", value=block_size_str)
    dace.Config.set("compiler", "cuda", "max_concurrent_streams", value="1")

    # Determine build configuration
    main_name = "main_gpu.cu" if stage >= 6 else "main.cu"
    is_lib = lib or (stage == 8 and options["build_for_integration"]) or stage == 9

    if stage == 1 and options["build_for_integration"]:
        for sdfg in sdfg_list:
            make_flattened_data_to_non_transient_cpu_input(sdfg)

    output_name = get_final_binary_name(stage, options)

    cmd = compile_if_propagated_sdfgs(
        sdfg_list,
        gpu=True,
        release=release,
        generate_code=True,
        lib=True,
        main_name=None,
        stage=stage,
        debuginfo=False,
        allocation_names_to_comment_out=allocation_names_to_comment_out,
        use_openacc_stream=use_openacc_stream,
        output_name=output_name,
    )

    print(f"Output available: {output_name}")
    print(f"Build command: {cmd}")
