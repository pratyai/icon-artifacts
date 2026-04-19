# SC2026 — Velocity Tendencies (VT)

**Scope**: only **stage 8** is relevant for the SC2026 results. Ignore
everything else in this directory.

## Prereqs
- Python 3.12, CUDA 12+, `ncu`
- `python3 -m venv .venv && source .venv/bin/activate`
- `pip install numpy h5py polars scipy netCDF4 tqdm git+https://github.com/spcl/dace.git@main`

## Data
- `data_r02b04/` reference dumps — download `data_r02b04.tar.zst` (412 MB) from
  <https://polybox.ethz.ch/index.php/s/xfprBf6rYjY7EZD>, then unpack:
  ```bash
  tar -I zstd -xf data_r02b04.tar.zst     # → data_r02b04/
  ```
  The public bundle contains only **timestep 2** (`*.2.*`) to keep the
  download small. Other timesteps + the `data_r02b0{3,5}/` grids live on Daint:
  `/capstor/scratch/cscs/pmazumde/gitspace/ico2/velocity/`.
- ICON grid `icon_grid_0010_R02B04_G.nc` — download from the DWD grid catalog:
  <http://icon-downloads.mpimet.mpg.de/dwd_grids.xml>

## Build (stage 8, release)

Six variants: {standalone, integration} × {fp64, fp32, fp16}.

### Standalone
```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --lowprec fp64 --reduce-bitwidth --lower-all
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --lowprec fp32 --reduce-bitwidth --lower-all
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --lowprec fp16 --reduce-bitwidth --lower-all
# → ./velocity_gpu.stage8_standalone_release.{fp64,fp32,fp16}
```

### Integration (ICON)
```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --lowprec fp64 --reduce-bitwidth --lower-all --integration
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --lowprec fp32 --reduce-bitwidth --lower-all --integration
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --lowprec fp16 --reduce-bitwidth --lower-all --integration
# → shared_libs/solve_nh_integration/release/8/libvelocity.so.{fp64,fp32,fp16}
```

## Run standalone
```bash
./velocity_gpu.stage8_standalone_release 2 --reps=3 --data=data_r02b04
#   2              : timestep
#   --reps=3       : repetitions for timing
#   --data=<dir>   : reference-dump directory (defaults to data_nproma<NPROMA>)
```

## Integrate with ICON
<!-- TODO: fill in the full ICON-side build + run recipe -->
1. Pick the integration build for the target precision, e.g.
   `shared_libs/solve_nh_integration/release/8/libvelocity.so.fp32`.
2. Point ICON at it — **TBD** (env var / symlink / ICON build-option path).
3. Build / rebuild ICON — **TBD**.
4. Run the ICON experiment — **TBD** (sbatch script or command).
5. Integration outputs land in — **TBD**.

## Validate (from ICON integration run)
<!-- TODO: replace with the concrete comparison against integration outputs -->
- Compare integration-run outputs against the reference — **TBD**
  (command, reference path, metric: SNR/RMSE).

## Profile (NCU)
```bash
sbatch profile_ncu.sh <precision> <timestep> <data_dir> <output_dir>
# e.g.
sbatch profile_ncu.sh f32 2 data_r02b03 tmp/vt_profiles-r02b03
# → <output_dir>/vt.f32.ncu-rep
```
Precision is `f16 | f32 | f64`.

## Gotchas
- `__dace_init_cuda` ODR bug: `utils/fix_init_cuda_odr.py` patches at compile; leave enabled.
- Boundary cast overflows `gridDim.z` at R02B06; use 4D split in `utils/boundary_cast.py`.
- FP16 builds require `include/fp16_operators.h` overloads + w_concorr FP32 override.
