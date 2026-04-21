import dace
import os
import shutil
import re
import argparse
import subprocess
from pathlib import Path

from dace.codegen import codegen, compiler
from dace.sdfg import infer_types

dace.config.Config.set("compiler", "default_data_types", value="C")
dace.config.Config.set("compiler", "cuda", "default_block_size", value="256,1,1")
dace.config.Config.set("compiler", "cuda", "max_concurrent_streams", value="1")

# --- Source Management ---

def repl_in_file(file_path: str, src: str, dst: str):
    if not os.path.exists(file_path):
        return
    with open(file_path, "r") as f:
        code = f.read()
    with open(file_path, "w") as f:
        f.write(code.replace(src, dst))


def stabilize_interface(header_path: str, main_cu: str):
    """Parse the generated header and rewrite cloudsc_main.cu call sites to match."""
    if not os.path.exists(header_path) or not os.path.exists(main_cu):
        return

    with open(header_path) as f:
        header = f.read()

    h = re.sub(r"\s+", " ", header)

    # Extract __dace_init param names
    m = re.search(r"__dace_init_cloudsc_py\(([^)]+)\)", h)
    if not m:
        print("  WARNING: could not parse __dace_init signature")
        return
    init_params = [p.strip().split()[-1] for p in m.group(1).split(",")]

    # Extract __program param names
    m = re.search(r"__program_cloudsc_py\(([^)]+)\)", h)
    if not m:
        print("  WARNING: could not parse __program signature")
        return
    prog_params = []
    for p in m.group(1).split(","):
        tokens = p.strip().replace("*", "").replace("__restrict__", "").split()
        prog_params.append(tokens[-1])

    init_call = f"__dace_init_cloudsc_py({', '.join(init_params)})"
    prog_call = f"__program_cloudsc_py({', '.join(prog_params)})"

    with open(main_cu) as f:
        code = f.read()

    code = re.sub(r"__dace_init_cloudsc_py\([^)]+\)", init_call, code)
    code = re.sub(r"__program_cloudsc_py\([^)]+\)", prog_call, code)

    with open(main_cu, "w") as f:
        f.write(code)

    print(f"  Stabilized interface: init({len(init_params)} args), program({len(prog_params)} args)")


def modify_files_in_directory(directory):
    pattern = re.compile(r"^(\s*)int tmp_struct_symbol")
    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            if file_path.endswith((".c", ".h", ".cpp", ".cu")):
                modify_file(file_path, pattern)


def modify_file(file_path, pattern):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    modified = False
    new_lines = []
    for line in lines:
        if pattern.match(line) and "(" not in line and "," not in line and ";" in line:
            line = pattern.sub(r"\1static int tmp_struct_symbol", line)
            modified = True
        new_lines.append(line)

    if modified:
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)


def _replace_cpp_with_cu(directory: Path):
    """Renames all .cpp and .cc files in a directory to .cu."""
    for ext in ["*.cpp", "*.cc"]:
        for file in directory.glob(ext):
            new_name = file.with_suffix(".cu")
            file.rename(new_name)
            print(f"  Renamed: {file.name} -> {new_name.name}")


def flatten_build_folder_gpu(build_loc: Path, sdfg_name: str):
    """Moves CPU and CUDA files to codegen/ and cleans up."""
    # Handle CPU files
    src_cpu = build_loc / "src" / "cpu"
    if src_cpu.exists():
        for f in src_cpu.glob("*"):
            shutil.move(str(f), str(build_loc.parent / f.name))

    # Handle CUDA files
    src_cuda = build_loc / "src" / "cuda"
    if src_cuda.exists():
        for f in src_cuda.glob("*"):
            shutil.move(str(f), str(build_loc.parent / f.name))

    # Handle headers
    include_path = build_loc / "include"
    if include_path.exists():
        for f in include_path.glob("*"):
            shutil.move(str(f), str(build_loc.parent / f.name))

    # Clean up
    shutil.rmtree(build_loc, ignore_errors=True)

    # Rename all .cpp to .cu in the flattened codegen directory
    _replace_cpp_with_cu(build_loc.parent)

    # Patch includes in all moved files
    for f in build_loc.parent.glob("*"):
        if f.suffix in (".cpp", ".cu", ".h"):
            repl_in_file(str(f), "../../include/hash.h", "hash.h")
            repl_in_file(str(f), f"../../include/{sdfg_name}.h", f"{sdfg_name}.h")


# --- Pipeline entry point ---

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdfg", type=str, default="cloudsc_opt.sdfgz")
    parser.add_argument("--release", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--lowprec", type=str, default="fp64",
                        choices=["fp64", "fp32", "fp16", "f64", "f32", "f16"])
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print(f"Loading SDFG from {args.sdfg}...")
    sdfg = dace.SDFG.from_file(args.sdfg)
    sdfg.name = "cloudsc_py"

    # Short precision tag: fp16 -> f16, etc.
    prec = args.lowprec.replace("fp", "f")

    codegen_dir = Path(f"build/codegen/{prec}")
    if codegen_dir.exists():
        shutil.rmtree(codegen_dir)
    codegen_dir.mkdir(parents=True)
    print(f"Created codegen directory: {codegen_dir.absolute()}")
    sdfg.build_folder = str(codegen_dir / sdfg.name)
    print(f"SDFG build folder set to: {sdfg.build_folder}")

    # 1. GPU Offloading (Your custom logic)
    print("Applying GPU offloading...")
    from gpu_offload import gpu_offload
    gpu_offload(sdfg, verbose=args.verbose)

    # 2. Precision Lowering
    print(f"Applying precision lowering: {args.lowprec}")
    from lowprec import apply_lowprec
    apply_lowprec(sdfg, args.lowprec)

    Path("build/sdfgz").mkdir(parents=True, exist_ok=True)
    lowered_sdfg_path = os.path.abspath(f"build/sdfgz/cloudsc_lowered_gpu.{prec}.sdfgz")
    sdfg.save(lowered_sdfg_path, compress=True)
    print(f"Saved lowered GPU SDFG to: {lowered_sdfg_path}")

    # 3. Codegen
    print("Generating GPU code...")
    sdfg.fill_scope_connectors()
    infer_types.infer_connector_types(sdfg)
    infer_types.set_default_schedule_and_storage_types(sdfg, None)
    sdfg.expand_library_nodes()
    infer_types.infer_connector_types(sdfg)
    infer_types.set_default_schedule_and_storage_types(sdfg, None)
    # 4. Symbol Propagation (Fixes mangled names by replacing symbols with their RHS)
    all_array_names = set()
    for s in sdfg.all_sdfgs_recursive():
        all_array_names.update(s.arrays.keys())
    for s in sdfg.all_sdfgs_recursive():
        # Find all assignments 'K = V' across all interstate edges
        defines = {}
        for edge in s.all_interstate_edges():
            if edge.data.assignments:
                for k, v in edge.data.assignments.items():
                    # Only propagate "Pure" symbols (math expressions).
                    # If the RHS contains a data access (like imelt[0]),
                    # replacing it into symbolic math breaks DaCe's C++ codegen.
                    # Also skip if RHS is an array name (creates symbol/array clash).
                    str_v = str(v)
                    if "[" not in str_v and "]" not in str_v and str_v not in all_array_names:
                        defines[str(k)] = str_v
        
        if defines:
            # Replace every occurrence of K with its RHS expression V
            for k, v in defines.items():
                s.replace(k, v)
            
            # Clean up: remove redundant assignments and symbol table entries
            for edge in s.all_interstate_edges():
                if edge.data.assignments:
                    for k in list(edge.data.assignments.keys()):
                        ks = str(k)
                        if ks in defines or not ks.isidentifier():
                            del edge.data.assignments[k]
            for k in defines:
                if k in s.symbols:
                    s.remove_symbol(k)


    program_objects = codegen.generate_code(sdfg, validate=False)
    compiler.generate_program_folder(sdfg, program_objects, sdfg.build_folder)

    build_loc = Path(sdfg.build_folder)
    modify_files_in_directory(build_loc)

    # 4. Flatten and Patch
    flatten_build_folder_gpu(build_loc, sdfg.name)
    
    from text_patches import apply_text_patches
    apply_text_patches(codegen_dir, args.lowprec)

    stabilize_interface(f"build/codegen/{prec}/cloudsc_py.h", "cloudsc_main.cu")

    # 5. Build Script Generation
    dace_runtime = Path(dace.__file__).parent / "runtime" / "include"

    # GPU Architecture detection (matches velocity)
    gencode_num = os.getenv("GENCODE_NUMBER")
    gencode_arch = os.getenv("GENCODE_ARCH")
    
    if gencode_arch:
        arch = gencode_arch
    elif gencode_num:
        arch = f"arch=compute_{gencode_num},code=sm_{gencode_num}"
    else:
        raise ValueError("GENCODE_NUMBER (e.g. 90) or GENCODE_ARCH must be set in environment.")

    # Ensure per-precision ptx_out directory exists
    ptx_dir = Path(f"build/ptx_out/{prec}")
    if not ptx_dir.exists():
        ptx_dir.mkdir(parents=True)

    h5_cflags = ""
    h5_libs = ""
    try:
        h5_cflags = subprocess.check_output(["pkg-config", "--cflags", "hdf5"], text=True).strip()
        h5_libs = subprocess.check_output(["pkg-config", "--libs", "hdf5"], text=True).strip()
    except Exception:
        pass # Handle manually if needed

    if args.release:
        nvcc_flags = (
            "-O3 -std=c++20 -DNDEBUG --use_fast_math --restrict "
            "-Xptxas=-O3 -Xptxas=-v --ftz=true --fmad=true "
            "--expt-relaxed-constexpr -lineinfo "
            "--prec-div=false --prec-sqrt=false"
        )
        xcompiler_flags = (
            "-O3 -march=native -mtune=native -DNDEBUG -Wall -Wextra "
            "-Wno-unused-parameter -Wconversion -Wno-sign-conversion "
            "-Wfloat-conversion -Wno-unknown-pragmas -faligned-new"
        )
    else:
        nvcc_flags = "-O0 -g -std=c++20 --expt-relaxed-constexpr -lineinfo"
        xcompiler_flags = "-O0 -Wall -Wextra -Wno-unused-parameter"
    
    # Suppress noisy DaCe-related CUDA warnings
    suppress = " ".join([f"--diag-suppress {x}" for x in [68, 550, 20208, 1835, 177, 20012, 1098]])
    
    # Per-precision outputs so builds coexist.
    bin_path = f"build/bin/cloudsc_gpu_bin.{prec}"
    sass_path = f"build/ptx_out/{prec}/all_kernels.sass"
    cmd = (
        f"nvcc {nvcc_flags} -gencode {arch} {suppress} \\\n"
        f"    -Xcompiler=\"{xcompiler_flags}\" \\\n"
        f"    -DCLOUDSC_PREC_TAG=\\\"{prec}\\\" \\\n"
        f"    --keep --keep-dir=build/ptx_out/{prec} \\\n"
        f"    -Xlinker --wrap=cudaMalloc -Xlinker --wrap=cudaFree \\\n"
        f"    -Ibuild/codegen/{prec} -Iinclude -I{dace_runtime} {h5_cflags} \\\n"
        f"    cloudsc_main.cu gpu_mem.cpp build/codegen/{prec}/*.cu \\\n"
        f"    -o {bin_path} {h5_libs} -lcudart -lpthread"
    )

    recompile_script = (
        f"#!/bin/bash\n"
        f"set -e\n"
        f"mkdir -p build/bin build/ptx_out/{prec}\n"
        f"rm -f {bin_path}\n"
        f"{cmd}\n"
        f"\n"
        f"# Dump SASS (native assembly) alongside PTX\n"
        f"echo 'Dumping SASS to {sass_path} ...'\n"
        f"cuobjdump -sass {bin_path} > {sass_path} || echo '  (SASS dump failed — non-fatal)'\n"
    )

    recompile_name = f"recompile.gpu.{prec}.sh"
    with open(recompile_name, "w") as f:
        f.write(recompile_script)
    os.chmod(recompile_name, 0o755)
    print(f"Build script written to: {os.path.abspath(recompile_name)}")
    print(f"Binary will be output to: {os.path.abspath(bin_path)}")
    print(f"\nGPU Pipeline ready. Build script: {recompile_name} (Release={args.release}, Precision={args.lowprec})")
    print(f"  Input SDFG:    {os.path.abspath(args.sdfg)}")
    print(f"  Lowered SDFG:  {lowered_sdfg_path}")
    print(f"  Codegen dir:   {codegen_dir.absolute()}")
    print(f"  Build script:  {os.path.abspath(recompile_name)}")
    print(f"  Binary output: {os.path.abspath(bin_path)}")


if __name__ == "__main__":
    main()
