# SC2026 — Velocity Tendencies (VT)

**Scope**: only **stage 8** is relevant for the SC2026 results. Ignore
everything else in this directory.

## 1. Get the code
Sparse-checkout only `velocity/` into a dir named `icon-vt-dace`:
```bash
git clone --depth 1 --filter=blob:none --sparse \
  --branch okbuddyicon git@github.com:pratyai/icon-artifacts.git _tmp_clone
cd _tmp_clone
git sparse-checkout set velocity
mv velocity ../icon-vt-dace
cd ..
rm -rf _tmp_clone
cd icon-vt-dace
```

## 2. Prereqs
- Python 3.12, CUDA 12+, `ncu`
- On the cluster:
  - One-time: pull the ICON uenv image to your local repo (auto-created
    on first use under `/capstor/scratch/cscs/$USER/.uenv-images`):
    ```bash
    uenv image pull icon/25.2:v1@santis
    ```
  - Every session: start the environment with the ICON view:
    ```bash
    uenv start --view=default icon/25.2:v1@santis
    ```
  - A working `spack` install, already activated in the shell (path/version
    depends on the user — `spack --version` should return something sensible).
  - Load the runtime deps:
    ```bash
    spack load cuda sqlite zstd
    ```
- `python3 -m venv .venv && source .venv/bin/activate`
- `pip install numpy h5py polars scipy netCDF4 tqdm git+https://github.com/spcl/dace.git@f2dace/staging`

## 3. Build integration `.so` + wrapper

Required env var (GH200):
```bash
export GENCODE_ARCH="arch=compute_90,code=sm_90"
```

Build the integration shared libraries (one per precision):
```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp64
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp32
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp16
# → libvelocity_gpu_stage8_solve_nh_integration_release.{fp64,fp32,fp16}.so
```

Regenerate the Fortran wrapper so its ABI matches the freshly-built `.so`.
Run once per precision you plan to integrate — the output file is a single
`wrapper.f90` (later runs overwrite earlier ones):
```bash
python gen_fortran_wrapper.py --stage-dir codegen/stage8/fp64 --serde --output wrapper.f90
python gen_fortran_wrapper.py --stage-dir codegen/stage8/fp32 --serde --output wrapper.f90
python gen_fortran_wrapper.py --stage-dir codegen/stage8/fp16 --serde --output wrapper.f90
# → wrapper.f90   (re-run with the precision you want to integrate next)
#   --serde is required: adds do_serialize-gated `serialize(at(...))` calls
#   (p_patch, p_prog.t0, p_metrics.t0, p_diag.t0, …) inside velocity_tendencies_gpu.
#   Without it, the GPU path produces no got/want dumps.
```

> **✅ Artifacts ready**
>
> At this point you have:
> - `libvelocity_gpu_stage8_solve_nh_integration_release.{fp64,fp32,fp16}.so`
> - `wrapper.f90`
> - `serde.f90` (serialization module, already in this tree;
>    mostly auto-generated + a small hand-written API block —
>    **TODO: streamline into a single fully auto-generated module**)
>
> **For the ICON-side build + run, continue in the icon-dace repo's
> `SC2026_HOWTO.md`.** The rest of this file (sections below) only covers
> the standalone profiling path — skip unless you're regenerating the
> NCU numbers.

## 4. Integrate with ICON
<!-- TODO: fill in the full ICON-side build + run recipe -->

ICON needs the R02B04 grid:
```bash
curl -LO http://icon-downloads.mpimet.mpg.de/grids/public/edzw/icon_grid_0010_R02B04_G.nc
# → icon_grid_0010_R02B04_G.nc
```
Full catalog: <http://icon-downloads.mpimet.mpg.de/dwd_grids.xml>

1. Pick the integration build for the target precision, e.g.
   `libvelocity_gpu_stage8_solve_nh_integration_release.fp32.so`.
2. Point ICON at it — **TBD** (env var / symlink / ICON build-option path).
3. Build / rebuild ICON — **TBD**.
4. Run the ICON experiment — **TBD** (sbatch script or command).
5. Integration outputs land in — **TBD**.

## 5. Validate (from ICON integration run)
<!-- TODO: replace with the concrete comparison against integration outputs -->
- Compare integration-run outputs against the reference — **TBD**
  (command, reference path, metric: SNR/RMSE).

## 6. Profile (standalone)

Everything in this section is only needed to regenerate the profiling
numbers in the paper (NCU table). Skip if you only care about the ICON
integration.

### 6.1 Reference data
`data_r02b04/` reference dumps (412 MB, timestep 2 only) from polybox:
```bash
curl -L -o data_r02b04.tar.zst \
  'https://polybox.ethz.ch/index.php/s/xfprBf6rYjY7EZD/download?files=data_r02b04.tar.zst'
tar -I zstd -xf data_r02b04.tar.zst
# → data_r02b04/
```
Other timesteps + `data_r02b0{3,5}/` grids live on Daint:
`/capstor/scratch/cscs/pmazumde/gitspace/ico2/velocity/`.

### 6.2 Build standalone
```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp64
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp32
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp16
# → ./velocity_gpu_stage8_standalone_release.{fp64,fp32,fp16}
```

### 6.3 Run standalone
```bash
./velocity_gpu_stage8_standalone_release.fp64 2 --reps=3 --data=data_r02b04
#   2              : timestep
#   --reps=3       : repetitions for timing
#   --data=<dir>   : reference-dump directory (defaults to data_nproma<NPROMA>)
```

### 6.4 Run NCU
```bash
sbatch profile_ncu.sh <precision> <timestep> <data_dir> <output_dir>
# e.g.
sbatch profile_ncu.sh f32 2 data_r02b04 vt_profiles-r02b04
# → <output_dir>/vt.f32.ncu-rep
```
Precision is `f16 | f32 | f64`. Inspect the report with `ncu-ui` or
`ncu --import <output_dir>/vt.<prec>.ncu-rep --page details`.

### 6.5 Metric mapping
The paper's profiling table is built from a handful of standard NCU
metrics — verified present in an NCU 2025.2 report:

- kernel duration: `gpu__time_duration.avg`
- DRAM read/write bytes: `dram__bytes_read.sum`, `dram__bytes_write.sum`
- BW utilization: `gpu__dram_throughput.avg.pct_of_peak_sustained_elapsed`
- L1 hit rate: `l1tex__t_sector_hit_rate.pct`
- L2 hit rate: `lts__t_sector_hit_rate.pct`
- occupancy: `sm__warps_active.avg.pct_of_peak_sustained_active`
- registers/thread: `launch__registers_per_thread`
- FP{64,32,16} FLOP counts: `derived__smsp__sass_thread_inst_executed_op_{d,f,h}{fma,mul,add}_pred_on_xN`
  (the `_xN` suffix pre-multiplies: FMA ×2, HFMA ×4 per NVIDIA convention)

Exact metric names shift across NCU versions. NCU 2025.2 in particular
re-namespaces raw-page CSV columns under groups like `FBSP.TriageCompute.*`,
`SM_B.TriageCompute.*`, etc., which breaks naive name-based extractors.
Easier to eyeball via `ncu --import <rep> --page details`.
