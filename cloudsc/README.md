# CloudSC DaCe Pipeline

Self-contained pipeline for optimizing, generating, and compiling the CloudSC microphysics scheme with DaCe. CPU (C++20) and GPU (CUDA) variants.

## Layout

Top level:
- `cloudsc_cpu_pipeline.py`, `cloudsc_gpu_pipeline.py` — pipeline entry points
- `optimize.py` — SSA + LoopToMap orchestrator (produces `build/sdfgz/after_*.sdfgz`)
- `cloudsc_main.cpp` / `cloudsc_main.cu` — CPU/GPU driver (include `cloudsc_py.h`, resolved via `-Ibuild/codegen`)
- `cloudsc_python_ref_run.py` — NumPy reference runner
- `compare_outputs.py` — validation (polars table, SNR, max rel err)
- `input.h5` — physical constants + input arrays
- `cloudsc_pydace_*.sdfgz` — input SDFGs (from `cloudsc_pydace.py`)
- `recompile.{cpu,gpu}.{f16,f32,f64}.sh` — one per variant×precision; gitignored

Under `build/` (gitignored, all per-precision so variants coexist):
- `build/codegen/{f16,f32,f64}/` — generated `.cpp` / `.cu` / `.h`
- `build/bin/cloudsc_{cpu,gpu}_bin.{f16,f32,f64}` — binaries
- `build/sdfgz/cloudsc_lowered{_gpu}.{f16,f32,f64}.sdfgz` — per-precision lowered SDFGs (plus shared `after_*.sdfgz` from `optimize.py`)
- `build/outputs/{cpp,gpu}/{f16,f32,f64}/` — HDF5 step outputs (ref is precision-free)
- `build/ptx_out/{f16,f32,f64}/` — nvcc `--keep` artifacts + `all_kernels.sass`

## Setup

Requires a sibling DaCe checkout at `../dace-cloudsc` on branch `pratyai/support-half`.

```bash
# One-time: clone the sibling dace on the required branch
git clone git@github.com:spcl/dace.git ../dace-cloudsc
cd ../dace-cloudsc
git checkout pratyai/support-half
git submodule update --init dace/external/cub dace/external/moodycamel dace/viewer/webclient
cd -

# Venv + deps
python3 -m venv .venv
source env.sh                      # or env.fish
pip install -r requirements.txt
pip install -e ../dace-cloudsc
```

System: `libhdf5` (brew or apt). GPU build additionally needs `nvcc` and a Hopper/Ampere device.

### On daint (uenv + spack)

```bash
uenv image pull icon/25.2:v1@santis                     # one-time
uenv start --view=default icon/25.2:v1@santis           # each session
spack load cuda sqlite zstd                             # provides nvcc, h5cc, mpicxx in PATH
export GENCODE_ARCH="arch=compute_90,code=sm_90"        # GH200
# then run Setup block above inside the uenv
```

HDF5 inside the uenv is parallel — the pipeline detects this by grepping `H5_HAVE_PARALLEL` in `H5pubconf.h` and pulls in MPI flags via `mpicxx --showme:compile`. Missing `mpicxx` on PATH → hard error (not a silent fallback).

## Optimize (once, precision-invariant)

`optimize.py` runs SSA + LoopToMap on the input SDFG. Output goes under `build/sdfgz/`.

```bash
python optimize.py cloudsc_pydace_simplified_symbolic.sdfgz
# -> build/sdfgz/cloudsc_pydace_simplified_symbolic_opt.sdfgz  (pipeline input below)
# -> build/sdfgz/after_*.sdfgz                                 (intermediate checkpoints)
```

Resume from a checkpoint: `python optimize.py <input> --start-from after_l2m`.

## Build

```bash
# CPU — one pipeline run per precision; precisions coexist
OPT=build/sdfgz/cloudsc_pydace_simplified_symbolic_opt.sdfgz
python cloudsc_cpu_pipeline.py --sdfg $OPT --release --lowprec fp64
./recompile.cpu.f64.sh                                    # -> build/bin/cloudsc_cpu_bin.f64
python cloudsc_cpu_pipeline.py --sdfg $OPT --release --lowprec fp32
./recompile.cpu.f32.sh                                    # -> build/bin/cloudsc_cpu_bin.f32

# GPU
GENCODE_NUMBER=90 python cloudsc_gpu_pipeline.py --sdfg $OPT --release --lowprec fp64
./recompile.gpu.f64.sh                                    # -> build/bin/cloudsc_gpu_bin.f64 + SASS dump
```

## Run

```bash
./build/bin/cloudsc_cpu_bin.f64 <steps> --save    # -> build/outputs/cpp/f64/
./build/bin/cloudsc_gpu_bin.f16 <steps> --save    # -> build/outputs/gpu/f16/
python cloudsc_python_ref_run.py --steps <n> --save  # -> build/outputs/ref/  (no precision tag)
```

## Profile (GPU, NCU)

Slurm wrapper — runs `ncu --set full` against the per-precision GPU binary. Output `.ncu-rep` goes under `build/profiles/` by default.

```bash
sbatch profile_ncu.sh f32 2                # -> build/profiles/cloudsc.f32.ncu-rep
sbatch profile_ncu.sh f16 2 build/profiles/custom-dir
```

Reuses velocity's post-processing: `velocity/extract_ncu.py` parses `.ncu-rep` into summary/aggregate CSVs; `velocity/report_ncu.py` prints the cold/warm table. FLOP sums are reconstructed via `.sum.per_cycle_elapsed × cycles_elapsed.avg` because NCU 2025.2.0 drops raw `.sum` when `--metrics` is used explicitly.

## Compare

```bash
python compare_outputs.py --prec f64                   # ref vs cpp f64, all steps
python compare_outputs.py --gpu --prec f16 --tol 1e-2  # ref vs gpu f16
python compare_outputs.py --step 5 --prec f32          # one step, cpu f32
```

## Precision

`--lowprec {fp64,fp32,fp16}` on either pipeline. ABI stays `double`; transients get lowered, boundary casts inserted. Bisection via env:

```bash
LOWER_ONLY=zqx   python cloudsc_gpu_pipeline.py --lowprec fp16
LOWER_NOTHING=1  python cloudsc_gpu_pipeline.py --lowprec fp16
```

## Optimize pipeline

```bash
python optimize.py cloudsc_pydace_simplified_symbolic.sdfgz
# -> build/sdfgz/cloudsc_pydace_simplified_symbolic_opt.sdfgz
# -> build/sdfgz/after_{liftcond,propagate,unroll,simplify1,ssa,isolate,privatize,expand,l2m,condfuse,condhoist,privatize2,l2m2,simplify}.sdfgz
```

Resume from a checkpoint with `--start-from after_l2m`.
