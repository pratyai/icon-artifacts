import dace
import os
import shutil
from pathlib import Path

from dace.codegen import codegen, compiler
from dace.sdfg import infer_types

from pipeline_utils import repl_in_file, stabilize_interface, modify_files_in_directory

dace.config.Config.set("compiler", "default_data_types", value="C")
dace.config.Config.set("compiler", "cpu", "openmp_sections", value=True)


# --- Source Management (matches velocity/utils/compile_if_propagated_sdfgs.py) ---


def flatten_build_folder(build_loc: Path, sdfg_name: str) -> tuple[str, str]:
    """
    Moves necessary files to the parent of build_loc and removes build_loc.
    Matches velocity's flatten_build_folder pattern.
    """
    cpu_path = None
    cpu_ext = "cpp"
    for ext in ["cpp", "cc"]:
        p = build_loc / "src" / "cpu" / f"{sdfg_name}.{ext}"
        if p.exists():
            cpu_path = p
            cpu_ext = ext
            break

    header_path = build_loc / "include" / f"{sdfg_name}.h"
    hash_path = build_loc / "include" / "hash.h"

    target_cpu = build_loc.parent / f"{sdfg_name}.{cpu_ext}"
    target_header = build_loc.parent / f"{sdfg_name}.h"
    target_hash = build_loc.parent / "hash.h"

    if cpu_path and cpu_path.exists():
        shutil.move(str(cpu_path), str(target_cpu))
    else:
        raise FileNotFoundError(
            f"Could not find {sdfg_name}.cpp in {build_loc / 'src' / 'cpu'}"
        )
    if header_path.exists():
        shutil.move(str(header_path), str(target_header))
    if hash_path.exists():
        shutil.move(str(hash_path), str(target_hash))

    # Clean up the original build folder
    shutil.rmtree(build_loc, ignore_errors=True)

    # Patch includes
    repl_in_file(str(target_cpu), "../../include/hash.h", "hash.h")
    repl_in_file(str(target_cpu), f"../../include/{sdfg_name}.h", f"{sdfg_name}.h")

    return str(target_cpu), str(target_header)


# --- Pipeline entry point ---


def main():
    import argparse
    import subprocess
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdfg", type=str, default="cloudsc_pydace_simplified_symbolic.sdfgz")
    parser.add_argument("--release", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--lowprec", type=str, default="fp64",
                        choices=["fp64", "fp32", "fp16", "f64", "f32", "f16"])
    args = parser.parse_args()
    print(f"Loading SDFG from {args.sdfg}...")

    sdfg = dace.SDFG.from_file(args.sdfg)
    sdfg.name = "cloudsc_py"

    # Short precision tag: fp16 -> f16, etc. Used to keep precisions coexisting.
    prec = args.lowprec.replace("fp", "f")

    # Per-precision codegen dir so f16/f32/f64 don't clobber each other.
    codegen_dir = Path(f"build/codegen/{prec}")
    if codegen_dir.exists():
        shutil.rmtree(codegen_dir)
    codegen_dir.mkdir(parents=True)
    sdfg.build_folder = str(codegen_dir / sdfg.name)

    print(f"Generating code ({'Release' if args.release else 'Debug'}, lowprec={args.lowprec})...")

    # SDFG-level precision lowering (before codegen)
    from lowprec import apply_lowprec
    apply_lowprec(sdfg, args.lowprec)

    # Save the lowered SDFG for inspection before codegen (per-precision)
    Path("build/sdfgz").mkdir(parents=True, exist_ok=True)
    lowered_sdfg_path = f"build/sdfgz/cloudsc_lowered.{prec}.sdfgz"
    sdfg.save(lowered_sdfg_path, compress=True)
    print(f"Saved lowered SDFG to {lowered_sdfg_path}")

    # Use velocity's lower-level codegen path
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

    # Flatten: move files up to codegen/ and clean up build subfolder
    _, header = flatten_build_folder(build_loc, sdfg.name)

    # Text-level patches on generated C++ (after codegen, before compile)
    from text_patches import apply_text_patches
    apply_text_patches(codegen_dir, args.lowprec)

    # Rewrite cloudsc_main.cpp call sites to match the generated interface
    stabilize_interface(str(header), "cloudsc_main.cpp")

    dace_runtime = Path(dace.__file__).parent / "runtime" / "include"

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

    # HDF5: REQUIRED (driver #includes hdf5.h). Prefer pkg-config, fall back to brew.
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

    # Is HDF5 parallel? Inspect H5pubconf.h in the HDF5 include dir — robust,
    # doesn't require h5cc on PATH.
    h5_parallel = None
    import re as _re
    include_dirs = _re.findall(r"-I(\S+)", h5_cflags)
    for inc in include_dirs:
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
    elif h5_parallel is None:
        print("  [probe] could not determine HDF5 parallelism; skipping MPI detection")

    # OpenMP: REQUIRED (generated code uses #pragma omp).
    # macOS: homebrew libomp. Linux: expect system libomp available.
    omp_cflags = ""
    omp_libs = "-lomp"
    omp_prefix = _probe("libomp prefix (brew, macOS only)", ["brew", "--prefix", "libomp"])
    if omp_prefix:
        omp_cflags = f"-I{omp_prefix}/include"
        omp_libs   = f"-L{omp_prefix}/lib -lomp"

    if args.release:
        cpp_flags = "-O3 -g -std=c++20 -DNDEBUG -Wall -Wextra -Wno-parentheses-equality -Wno-unused-parameter -Wno-unknown-pragmas -Xpreprocessor -fopenmp"
    else:
        cpp_flags = "-O0 -g -std=c++20 -Wall -Wextra -Wno-parentheses-equality -Wno-unused-parameter -Wno-unknown-pragmas -Xpreprocessor -fopenmp"

    bin_path = f"build/bin/cloudsc_cpu_bin.{prec}"
    cmd = (
        f"c++ {cpp_flags} {omp_cflags} \\\n"
        f"    -DCLOUDSC_PREC_TAG=\\\"{prec}\\\" \\\n"
        f"    -Ibuild/codegen/{prec} -Iinclude -I{dace_runtime} {h5_cflags} {mpi_cflags} \\\n"
        f"    cloudsc_main.cpp build/codegen/{prec}/*.cpp \\\n"
        f"    -o {bin_path} -lpthread {omp_libs} {h5_libs} {mpi_libs}"
    )

    recompile_script = f"""#!/bin/bash
set -e

# Ensure output dir exists
mkdir -p build/bin

# Remove old binary if it exists
rm -f {bin_path}

# Compile
{cmd}
"""
    recompile_name = f"recompile.cpu.{prec}.sh"
    with open(recompile_name, "w") as f:
        f.write(recompile_script)
    os.chmod(recompile_name, 0o755)
    print(f"Pipeline ready. Build script: {recompile_name} -> {bin_path} (Release={args.release})")


if __name__ == "__main__":
    main()
