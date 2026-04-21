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

Requires DaCe from `../dace-cloudsc` on branch `pratyai/support-half`.

```bash
python3 -m venv .venv
source env.sh
pip install -r requirements.txt
pip install -e ../dace-cloudsc
```

System: `libhdf5` (brew or apt). GPU build additionally needs `nvcc` and a Hopper/Ampere device.

## Build

```bash
# CPU — one pipeline run per precision; precisions coexist
python cloudsc_cpu_pipeline.py --release --lowprec fp64
./recompile.cpu.f64.sh                                    # -> build/bin/cloudsc_cpu_bin.f64
python cloudsc_cpu_pipeline.py --release --lowprec fp32
./recompile.cpu.f32.sh                                    # -> build/bin/cloudsc_cpu_bin.f32

# GPU
GENCODE_NUMBER=90 python cloudsc_gpu_pipeline.py --release --lowprec fp64
./recompile.gpu.f64.sh                                    # -> build/bin/cloudsc_gpu_bin.f64 + SASS dump
```

## Run

```bash
./build/bin/cloudsc_cpu_bin.f64 <steps> --save    # -> build/outputs/cpp/f64/
./build/bin/cloudsc_gpu_bin.f16 <steps> --save    # -> build/outputs/gpu/f16/
python cloudsc_python_ref_run.py --steps <n> --save  # -> build/outputs/ref/  (no precision tag)
```

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
