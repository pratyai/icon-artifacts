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
./run_snr_compare.sh <GRID> [EXP_DIR] [DB]
# defaults: EXP_DIR=../icon-dace/build/verification/experiments, DB=./snr.db
```

Examples:
```bash
./run_snr_compare.sh 0050_R02B03   # 320 km, default EXP_DIR + snr.db
./run_snr_compare.sh 0010_R02B04
./run_snr_compare.sh 0008_R02B05
./run_snr_compare.sh 0002_R02B06
```

If icon-dace isn't a sibling of this VT tree (e.g. on Daint the VT tree
may live under `sc2026-ad-test/` while icon-dace lives under
`gitspace/`), pass the absolute `EXP_DIR` explicitly:

```bash
./run_snr_compare.sh 0010_R02B04 /abs/path/to/icon-dace/build/verification/experiments
```

Writes a shared `snr.db` (SQLite), keyed by
`(grid, tag, phys, field, sub_field)`. Tags per grid:
`FP32_vs_FP64`, `FP16_vs_FP64`, `FP64_vs_refined`, `FP32_vs_refined`,
`FP16_vs_refined`.

### Paper table (first-step SNR)

The paper reports SNR at the first physics generation — `phys=2` in our
data (phys=0/1 are init / pre-step). `report_serde.py --phys N` emits a
polars pivot per grid, rows = paper's 5 fields (`vn`, `w`, `vt`,
`vn_ie`, `w_concorr_c`), columns = tags:

```bash
python -m utils.report_serde snr.db --phys 2 --cross-only
# → PAPER SNR TABLE: grid=R02B04, phys=2
#   field       │ FP32   │ FP16  │ FP64_vs_refined │ FP32_vs_refined │ FP16_vs_refined
#   vn          │ 154.10 │ 75.80 │ 66.60           │ 66.60           │ 66.10
#   ...
```

Column legend (REF → TEST):

| Tag                 | REF                        | TEST                       | Interpretation                             |
|---------------------|----------------------------|----------------------------|--------------------------------------------|
| `FP32_vs_FP64`      | `ss5_vanilla` (FP64 ref)   | `ss5_gpufp32` (VT FP32)    | noise FP32 introduces                      |
| `FP16_vs_FP64`      | `ss5_vanilla` (FP64 ref)   | `ss5_gpufp16` (VT FP16)    | noise FP16 introduces                      |
| `FP64_vs_refined`   | `ss5_vanilla` (FP64 ref)   | `ss10_vanilla` (FP64 ref×2 substeps) | temporal-discretization floor    |
| `FP32_vs_refined`   | `ss10_vanilla` (refined)   | `ss5_gpufp32` (VT FP32)    | FP32 vs the refined reference              |
| `FP16_vs_refined`   | `ss10_vanilla` (refined)   | `ss5_gpufp16` (VT FP16)    | FP16 vs the refined reference              |

Viability check (paper convention): FP32/FP16 must beat `FP64_vs_refined` — otherwise
reduced-precision noise has dipped below the temporal-discretization floor.

One pivot is emitted per grid in the DB.

## 6. Profile (standalone)

Only needed to regenerate the paper's NCU numbers. Skip for ICON
integration.

### 6.1 Reference data
All four grids available on polybox (folder `SC2026 Data Dumps`).
Timestep is 2 for R02B03/04/05, 1 for R02B06 (R02B06 was too big to
keep more steps):

```bash
SHARE='https://polybox.ethz.ch/index.php/s/xfprBf6rYjY7EZD/download'
for F in data_r02b03.tar.zst data_r02b04.tar.zst data_r02b05.tar.zst data_r02b06.tar.zst; do
  curl -L -o "$F" "$SHARE?files=$F"
  tar -I zstd -xf "$F"
done
# → data_r02b0{3,4,5,6}/
```

Extra timesteps beyond these remain only on Daint at
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

### 6.4 NCU — submit

`profile_ncu.sh <precision> <timestep> <data_dir> <output_dir>` profiles
one (precision, grid). Full 4×3 = 12-job sweep. Timestep is 2 for
R02B03/04/05 and 1 for R02B06 (matches the polybox tarballs):

```bash
# 320 km (R02B03) — timestep 2
sbatch profile_ncu.sh f64 2 data_r02b03 vt_profiles-r02b03
sbatch profile_ncu.sh f32 2 data_r02b03 vt_profiles-r02b03
sbatch profile_ncu.sh f16 2 data_r02b03 vt_profiles-r02b03

# 160 km (R02B04) — timestep 2
sbatch profile_ncu.sh f64 2 data_r02b04 vt_profiles-r02b04
sbatch profile_ncu.sh f32 2 data_r02b04 vt_profiles-r02b04
sbatch profile_ncu.sh f16 2 data_r02b04 vt_profiles-r02b04

# 80 km (R02B05) — timestep 2
sbatch profile_ncu.sh f64 2 data_r02b05 vt_profiles-r02b05
sbatch profile_ncu.sh f32 2 data_r02b05 vt_profiles-r02b05
sbatch profile_ncu.sh f16 2 data_r02b05 vt_profiles-r02b05

# 40 km (R02B06) — timestep 1 (only step in the tarball)
sbatch profile_ncu.sh f64 1 data_r02b06 vt_profiles-r02b06
sbatch profile_ncu.sh f32 1 data_r02b06 vt_profiles-r02b06
sbatch profile_ncu.sh f16 1 data_r02b06 vt_profiles-r02b06
# → vt_profiles-<grid>/vt.<precision>.ncu-rep  (12 files total)
```

### 6.5 NCU — extract once, report many

`extract_ncu.py` shells out to `ncu` to parse each `.ncu-rep` — slow
(tens of seconds per file) and serial on NCU's side. Run it **once**
over all 12 reports; it writes `ncu_summary.csv` (per-kernel) and
`ncu_aggregate.csv` (per-file totals). `report_ncu.py` then reads those
CSVs — fast, no re-parsing:

```bash
python extract_ncu.py vt_profiles-r02b0*/vt.*.ncu-rep   # one-shot, writes 2 CSVs
python report_ncu.py                                     # aggregate + dominant kernel
python report_ncu.py r02b04 fp16                         # filter by grid/precision
python report_ncu.py --detail r02b04                     # per-kernel detail
```

Re-run `extract_ncu.py` only when you add new `.ncu-rep` files.
Inspect any single report directly: `ncu-ui <file.ncu-rep>` or
`ncu --import <file.ncu-rep> --page details`.

### 6.6 Metric mapping
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
