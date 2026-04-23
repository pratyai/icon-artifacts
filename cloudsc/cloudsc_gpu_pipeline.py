import dace
import os
import shutil
import argparse
import subprocess
from pathlib import Path

from dace.codegen import codegen, compiler
from dace.sdfg import infer_types

from pipeline_utils import repl_in_file, stabilize_interface, modify_files_in_directory

dace.config.Config.set("compiler", "default_data_types", value="C")
dace.config.Config.set("compiler", "cuda", "default_block_size", value="256,1,1")
dace.config.Config.set("compiler", "cuda", "max_concurrent_streams", value="1")

# --- Source Management ---

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

    # ---- Frontload toolchain probes: fail fast BEFORE any codegen. ----
    prec = args.lowprec.replace("fp", "f")

    def _probe(label, cmd):
        """Run a detector command; print what we got so nothing is silent."""
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
            print(f"  [probe] {label}: {' '.join(cmd)!r} -> {out or '(empty)'}")
            return out
        except FileNotFoundError as e:
            print(f"  [probe] {label}: {cmd[0]!r} not on PATH ({e}); leaving empty")
        except subprocess.CalledProcessError as e:
            print(f"  [probe] {label}: {' '.join(cmd)!r} exited {e.returncode}; stderr/stdout:\n{e.output}")
        return ""

    # GPU architecture
    gencode_num = os.getenv("GENCODE_NUMBER")
    gencode_arch = os.getenv("GENCODE_ARCH")
    if gencode_arch:
        arch = gencode_arch
    elif gencode_num:
        arch = f"arch=compute_{gencode_num},code=sm_{gencode_num}"
    else:
        raise ValueError("GENCODE_NUMBER (e.g. 90) or GENCODE_ARCH must be set in environment.")
    print(f"  [probe] GPU arch: {arch}")

    dace_runtime = Path(dace.__file__).parent / "runtime" / "include"
    print(f"  [probe] dace runtime include: {dace_runtime}")

    # HDF5 — required.
    h5_cflags = _probe("hdf5 cflags (pkg-config)", ["pkg-config", "--cflags", "hdf5"])
    h5_libs   = _probe("hdf5 libs (pkg-config)",   ["pkg-config", "--libs",   "hdf5"])
    if not h5_cflags:
        h5_prefix = _probe("hdf5 prefix (brew)", ["brew", "--prefix", "hdf5"])
        if h5_prefix:
            h5_cflags = f"-I{h5_prefix}/include"
            h5_libs   = f"-L{h5_prefix}/lib -lhdf5"
    if not h5_cflags or not h5_libs:
        raise RuntimeError(
            "HDF5 not found. Install it (brew install hdf5 / module load hdf5) "
            "or ensure pkg-config / brew can locate it."
        )

    # Parallel HDF5 detection via H5pubconf.h.
    h5_parallel = None
    import re as _re
    for inc in _re.findall(r"-I(\S+)", h5_cflags):
        pubconf = Path(inc) / "H5pubconf.h"
        if pubconf.exists():
            text = pubconf.read_text(errors="ignore")
            h5_parallel = bool(_re.search(r"^\s*#define\s+H5_HAVE_PARALLEL\s+1", text, _re.M))
            print(f"  [probe] parallel HDF5 (from {pubconf}): {h5_parallel}")
            break
    else:
        cfg = _probe("hdf5 build config (h5cc fallback)", ["h5cc", "-showconfig"])
        if cfg:
            h5_parallel = "Parallel HDF5: yes" in cfg
            print(f"  [probe] parallel HDF5 (h5cc): {h5_parallel}")

    mpi_cflags = ""
    mpi_libs = ""
    if h5_parallel:
        env_cf = os.getenv("CLOUDSC_MPI_CFLAGS")
        env_lf = os.getenv("CLOUDSC_MPI_LIBS")
        if env_cf is not None or env_lf is not None:
            mpi_cflags = env_cf or ""
            mpi_libs   = env_lf or ""
            print(f"  [probe] mpi cflags (CLOUDSC_MPI_CFLAGS): {mpi_cflags or '(empty)'}")
            print(f"  [probe] mpi libs   (CLOUDSC_MPI_LIBS):   {mpi_libs or '(empty)'}")
        else:
            mpi_cflags = _probe("mpi cflags (openmpi)", ["mpicxx", "--showme:compile"])
            mpi_libs   = _probe("mpi libs (openmpi)",   ["mpicxx", "--showme:link"])
            if not mpi_cflags or not mpi_libs:
                print("  [probe] openmpi-style failed; trying MPICH-style 'mpicxx -show'")
                show = _probe("mpi show (mpich)", ["mpicxx", "-show"])
                if show:
                    cf_toks, lib_toks = [], []
                    for tok in show.split():
                        if tok.startswith(("-I", "-D")):
                            cf_toks.append(tok)
                        elif tok.startswith(("-L", "-l", "-Wl,")):
                            lib_toks.append(tok)
                    mpi_cflags = " ".join(cf_toks)
                    mpi_libs   = " ".join(lib_toks)
                    print(f"  [probe] mpi cflags (parsed from -show): {mpi_cflags or '(empty)'}")
                    print(f"  [probe] mpi libs   (parsed from -show): {mpi_libs or '(empty)'}")
        if not mpi_cflags or not mpi_libs:
            raise RuntimeError(
                "HDF5 is parallel but MPI flags could not be resolved. Tried OpenMPI wrapper "
                "(`mpicxx --showme:compile/link`) and MPICH wrapper (`mpicxx -show`). "
                "Set CLOUDSC_MPI_CFLAGS and CLOUDSC_MPI_LIBS to bypass detection."
            )
        # nvcc-wrap non-standard flags. -I/-L/-l/-D/-U pass through; -Wl,foo,bar
        # becomes -Xlinker=foo -Xlinker=bar; anything else gets -Xcompiler=.
        def _nvccify(flagstr: str) -> str:
            out = []
            for tok in flagstr.split():
                if tok.startswith(("-I", "-L", "-l", "-D", "-U")):
                    out.append(tok)
                elif tok.startswith("-Wl,"):
                    for piece in tok[4:].split(","):
                        if piece:
                            out.append(f"-Xlinker={piece}")
                else:
                    out.append(f"-Xcompiler={tok}")
            return " ".join(out)
        mpi_cflags = _nvccify(mpi_cflags)
        mpi_libs   = _nvccify(mpi_libs)
        print(f"  [probe] mpi cflags (nvcc-wrapped): {mpi_cflags}")
        print(f"  [probe] mpi libs   (nvcc-wrapped): {mpi_libs}")
    elif h5_parallel is None:
        print("  [probe] could not determine HDF5 parallelism; skipping MPI detection")

    # ---- End toolchain probes. Now safe to proceed to codegen. ----

    print(f"Loading SDFG from {args.sdfg}...")
    sdfg = dace.SDFG.from_file(args.sdfg)
    sdfg.name = "cloudsc_py"

    codegen_dir = Path(f"build/codegen/{prec}")
    if codegen_dir.exists():
        shutil.rmtree(codegen_dir)
    codegen_dir.mkdir(parents=True)
    print(f"Created codegen directory: {codegen_dir.absolute()}")
    sdfg.build_folder = str(codegen_dir / sdfg.name)
    print(f"SDFG build folder set to: {sdfg.build_folder}")

    # 0b. Zero-init transients that are read before any write. Must run
    #     BEFORE gpu_offload so the newly-inserted init state and its maps
    #     get the same GPU schedule/device-memory treatment as everything
    #     else; otherwise we'd be zeroing host memory while kernels read
    #     device memory.
    from ssa.zero_init_transients import zero_init_uninitialized_transients
    nz = zero_init_uninitialized_transients(sdfg)
    if nz:
        print(f"Zero-initialized {nz} uninitialized transients")

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

    # Per-precision ptx_out directory (for nvcc --keep)
    ptx_dir = Path(f"build/ptx_out/{prec}")
    if not ptx_dir.exists():
        ptx_dir.mkdir(parents=True)

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
        f"    -Ibuild/codegen/{prec} -Iinclude -I{dace_runtime} {h5_cflags} {mpi_cflags} \\\n"
        f"    cloudsc_main.cu gpu_mem.cpp build/codegen/{prec}/*.cu \\\n"
        f"    -o {bin_path} {h5_libs} {mpi_libs} -lcudart -lpthread"
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
