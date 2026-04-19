# SC2026 — Velocity Tendencies (VT)

**Scope**: only **stage 8** is relevant for SC2026. Ignore everything else.

## 1. Get the code
Sparse-checkout `velocity/` into `icon-vt-dace/`:
```bash
git clone --depth 1 --filter=blob:none --sparse \
  --branch okbuddyicon git@github.com:pratyai/icon-artifacts.git _tmp_clone
cd _tmp_clone
git sparse-checkout set velocity
mv velocity ../icon-vt-dace
cd .. && rm -rf _tmp_clone && cd icon-vt-dace
```

## 2. Prereqs
- Python 3.12, CUDA 12+, `ncu`
- On the cluster:
  - `uenv image pull icon/25.2:v1@santis`  (one-time)
  - `uenv start --view=default icon/25.2:v1@santis`  (each session)
  - `spack` activated (`spack --version` should work)
  - `spack load cuda sqlite zstd`
- `python3 -m venv .venv && source .venv/bin/activate`
- `pip install numpy h5py polars scipy netCDF4 tqdm zstandard git+https://github.com/spcl/dace.git@f2dace/staging`

## 3. Build integration `.so` + wrapper

GH200:
```bash
export GENCODE_ARCH="arch=compute_90,code=sm_90"
```

One `.so` per precision:
```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp64
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp32
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp16
# → libvelocity_gpu_stage8_solve_nh_integration_release.{fp64,fp32,fp16}.so
```

Regenerate the Fortran wrapper (one file; later runs overwrite):
```bash
python gen_fortran_wrapper.py --stage-dir codegen/stage8/fp64 --serde --output wrapper.f90
python gen_fortran_wrapper.py --stage-dir codegen/stage8/fp32 --serde --output wrapper.f90
python gen_fortran_wrapper.py --stage-dir codegen/stage8/fp16 --serde --output wrapper.f90
# → wrapper.f90   (re-run with the precision you'll integrate next)
```
`--serde` is required — adds the `do_serialize`-gated `serialize(at(...))`
calls inside `velocity_tendencies_gpu`. Without it the GPU path dumps
nothing.

> **✅ Artifacts ready**
>
> - `libvelocity_gpu_stage8_solve_nh_integration_release.{fp64,fp32,fp16}.so`
> - `wrapper.f90`
> - `serde.f90` (already in-tree; mostly auto-generated, small hand-written
>   API block — **TODO: fully auto-generate**)
>
> **Next: §4 hands off to icon-dace for data generation, §5 brings you
> back here for analysis.** §6 is the standalone NCU-profiling path,
> independent of §4–5 — skip unless regenerating the paper's NCU numbers.

## 4. Generate data (over in icon-dace)

Data generation — ICON build, submit, `.data` dumps — lives in
**icon-dace's `SC2026_HOWTO.md`**. In short: copy `wrapper.f90` into
`icon-dace/src/atm_dyn_iconam/`, build ICON, then run
`./run/sbatch_all_sc2026.sh <GRID>` over there for each grid.

Follow it through §8 of icon-dace's HOWTO, then come back here for
analysis.

## 5. Analysis — SNR (come back from icon-dace)

After jobs land their `.data` files under
`icon-dace/build/verification/experiments/`, run from this tree:

```bash
./run_snr_compare.sh 0050_R02B03   # 320 km
./run_snr_compare.sh 0010_R02B04   # 160 km
./run_snr_compare.sh 0008_R02B05   #  80 km
./run_snr_compare.sh 0002_R02B06   #  40 km
# → snr.db
```

Writes a shared `snr.db` (SQLite), keyed by
`(grid, tag, phys, field, sub_field)`. Tags per grid:
`FP32_vs_FP64`, `FP16_vs_FP64`, `FP64_vs_refined`, `FP32_vs_refined`,
`FP16_vs_refined`.

## 6. Profile (standalone)

Only needed to regenerate the paper's NCU numbers. Skip for ICON
integration.

### 6.1 Reference data
`data_r02b04/` (412 MB, timestep 2 only) from polybox:
```bash
curl -L -o data_r02b04.tar.zst \
  'https://polybox.ethz.ch/index.php/s/xfprBf6rYjY7EZD/download?files=data_r02b04.tar.zst'
tar -I zstd -xf data_r02b04.tar.zst
```
Other timesteps + `data_r02b0{3,5}/` on Daint at
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
#   2         : timestep
#   --reps    : timing repetitions
#   --data    : reference-dump dir (defaults to data_nproma<NPROMA>)
```

### 6.4 NCU
```bash
sbatch profile_ncu.sh <precision> <timestep> <data_dir> <output_dir>
# e.g.
sbatch profile_ncu.sh f32 2 data_r02b04 vt_profiles-r02b04
# → <output_dir>/vt.f32.ncu-rep
```
Precision: `f16 | f32 | f64`. Inspect via `ncu-ui` or
`ncu --import <rep> --page details`.

### 6.5 Metric mapping
Paper table metrics (verified on NCU 2025.2):

- duration: `gpu__time_duration.avg`
- DRAM R/W: `dram__bytes_read.sum`, `dram__bytes_write.sum`
- BW util: `gpu__dram_throughput.avg.pct_of_peak_sustained_elapsed`
- L1 / L2 hit rate: `l1tex__t_sector_hit_rate.pct`, `lts__t_sector_hit_rate.pct`
- occupancy: `sm__warps_active.avg.pct_of_peak_sustained_active`
- regs/thread: `launch__registers_per_thread`
- FP64/32/16 FLOPs: `derived__smsp__sass_thread_inst_executed_op_{d,f,h}{fma,mul,add}_pred_on_xN`
  (`_xN` pre-multiplies — FMA×2, HFMA×4 per NVIDIA)

Names shift across NCU versions. 2025.2 re-namespaces raw-page CSV
columns under `FBSP.TriageCompute.*`, `SM_B.TriageCompute.*`, etc., which
breaks naive extractors — eyeball via `--page details`.
