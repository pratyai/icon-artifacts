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

    codegen_dir = Path("codegen")
    if codegen_dir.exists():
        shutil.rmtree(codegen_dir)
    codegen_dir.mkdir()
    sdfg.build_folder = str(codegen_dir / sdfg.name)

    # 1. GPU Offloading (Your custom logic)
    print("Applying GPU offloading...")
    from gpu_offload import gpu_offload
    gpu_offload(sdfg, verbose=args.verbose)

    # 2. Precision Lowering
    print(f"Applying precision lowering: {args.lowprec}")
    from lowprec import apply_lowprec
    apply_lowprec(sdfg, args.lowprec)

    sdfg.save("cloudsc_lowered_gpu.sdfgz", compress=True)
    print("Saved lowered GPU SDFG to cloudsc_lowered_gpu.sdfgz")

    # 3. Codegen
    print("Generating GPU code...")
    sdfg.fill_scope_connectors()
    infer_types.infer_connector_types(sdfg)
    infer_types.set_default_schedule_and_storage_types(sdfg, None)
    sdfg.expand_library_nodes()
    infer_types.infer_connector_types(sdfg)
    infer_types.set_default_schedule_and_storage_types(sdfg, None)

    program_objects = codegen.generate_code(sdfg, validate=False)
    compiler.generate_program_folder(sdfg, program_objects, sdfg.build_folder)

    build_loc = Path(sdfg.build_folder)
    modify_files_in_directory(build_loc)

    # 4. Flatten and Patch
    flatten_build_folder_gpu(build_loc, sdfg.name)
    
    from text_patches import apply_text_patches
    apply_text_patches(codegen_dir, args.lowprec)

    stabilize_interface("codegen/cloudsc_py.h", "cloudsc_main.cu")

    # 5. Build Script Generation
    dace_runtime = Path(dace.__file__).parent / "runtime" / "include"

    h5_cflags = ""
    h5_libs = ""
    try:
        h5_cflags = subprocess.check_output(["pkg-config", "--cflags", "hdf5"], text=True).strip()
        h5_libs = subprocess.check_output(["pkg-config", "--libs", "hdf5"], text=True).strip()
    except Exception:
        pass # Handle manually if needed

    nvcc_flags = "-O3 -std=c++20" if args.release else "-O0 -g -std=c++20"
    
    # We include all .cpp and .cu files in codegen/
    cmd = (
        f"nvcc {nvcc_flags} \\\n"
        f"    -Icodegen -Iinclude -I{dace_runtime} {h5_cflags} \\\n"
        "    cloudsc_main.cu codegen/*.cpp codegen/*.cu \\\n"
        f"    -o cloudsc_gpu_bin {h5_libs} -lcudart -lpthread"
    )

    recompile_script = f"#!/bin/bash\nset -e\nrm -f cloudsc_gpu_bin\n{cmd}\n"
    
    with open("recompile.sh", "w") as f:
        f.write(recompile_script)
    os.chmod("recompile.sh", 0o755)
    print(f"GPU Pipeline ready. Build command in recompile.sh (Release={args.release}, Precision={args.lowprec})")


if __name__ == "__main__":
    main()
