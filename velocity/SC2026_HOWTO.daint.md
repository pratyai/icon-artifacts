# SC2026 — Velocity Tendencies (VT)

**Scope**: only **stage 8** is relevant for SC2026. Ignore everything else.

## Prerequisites

### Spack (one-time)

Spack and its packages repo live in two clones. Pick any location; the example
uses `$SCRATCH` (on CSCS Alps this resolves to `/capstor/scratch/cscs/$USER`, a
purge-eligible fast tier — fine for a spack tree, not for anything you want to
keep long-term).

```bash
export SPACK_TREE=$SCRATCH/spack-tree
mkdir -p $SPACK_TREE && cd $SPACK_TREE
git clone --depth=1 https://github.com/spack/spack.git
git clone --depth=1 https://github.com/spack/spack-packages.git

source $SPACK_TREE/spack/share/spack/setup-env.sh
spack repo remove builtin 2>/dev/null || true   # drop any stale user-scoped entry
spack repo add $SPACK_TREE/spack-packages/repos/spack_repo/builtin
```

Add the `source` + `spack repo add` lines to your shell rc so future shells see
them.

### Get the code (one-time)

Sparse-checkout `velocity/`, keeping the clone so later updates are a `git
pull` rather than a re-clone (which would take `.venv` and any build products
with it):

```bash
git clone --depth 1 --filter=blob:none --sparse \
  --branch okbuddyicon https://github.com/pratyai/icon-artifacts.git icon-vt-dace
cd icon-vt-dace
git sparse-checkout set velocity
cd velocity
```

All subsequent commands assume you are in `icon-vt-dace/velocity`.

`icon-dace` is then no longer a sibling of this directory, so §3 needs its
`EXP_DIR` passed explicitly.

### VT spack env (one-time)

Externals in `arch/cscs/daint/spack.yaml` bind to paths under
`/user-environment`, only mounted while the `icon/25.2:v1@santis` uenv is
active. Every `spack` invocation that touches those externals (`concretize`,
`install`, `load`) must therefore run inside a uenv shell.

Enter one for the whole session:

```bash
uenv image pull icon/25.2:v1@santis             # one-time
uenv start --view=default icon/25.2:v1@santis
```

Then, from inside the uenv shell, re-source spack (uenv resets `PATH`) and
create + build the env:

```bash
source $SPACK_TREE/spack/share/spack/setup-env.sh
spack env create vt-gpu ./arch/cscs/daint/spack.yaml
spack -e vt-gpu concretize
spack -e vt-gpu install
spack env activate vt-gpu
```

The env pins CUDA to the uenv's 12.6 toolkit as an external, and provides the
`sqlite3`, `zlib` and `libzstd` that the `.so` links against. Activating it is
what puts them on `pkg-config`'s path for §1.

For SLURM jobs, use `#SBATCH --uenv=icon/25.2:v1@santis` + `#SBATCH
--view=default` in the script header — submitting `sbatch` from inside a uenv
shell is blocked with `libslurm-uenv-mount rc=-3000`.

### Python venv (one-time)

Daint's system `python3` is 3.6 and lives on the frontend only; the uenv's is
3.10. For a 3.12 interpreter that is also visible on compute nodes, let uv
manage it (`--python-preference only-managed` keeps the venv off the system
python):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"            # add to shell rc too

uv python install 3.12
uv venv --python 3.12 --python-preference only-managed .venv
source .venv/bin/activate
uv pip install numpy h5py polars scipy netCDF4 tqdm zstandard
```

DaCe must be installed from a clone, editable. Keep the clone outside the
checkout, next to `icon-vt-dace`:

```bash
git clone --recursive -b f2dace/staging https://github.com/spcl/dace.git ../../dace
uv pip install -e ../../dace
```

`--recursive` matters: DaCe carries its externals (`cub`, `rtllib`, `hlslib`,
…) as submodules, and importing `dace.transformation` reaches
`dace.external.rtllib.templates` through the codegen targets. Without them the
import fails.

Installing it straight from the URL (`uv pip install git+…dace.git@f2dace/staging`)
builds a wheel instead, and `setup.py` selects modules with `find_packages()`,
which skips subpackages that carry no `__init__.py` —
`dace.transformation.passes.simplification` is one, so the import fails at
`compile_gpu_stage8` time. An editable install maps the source tree directly
and keeps them.

`ncu` is needed only for the profiling path (§4).

## Every login

```bash
export SPACK_TREE=$SCRATCH/spack-tree
export PATH="$HOME/.local/bin:$PATH"

uenv start --view=default icon/25.2:v1@santis    # resets PATH — source spack after it
source $SPACK_TREE/spack/share/spack/setup-env.sh
spack env activate vt-gpu
source .venv/bin/activate                        # from icon-vt-dace/velocity
```

Order matters: `uenv start` resets `PATH`, so spack has to be re-sourced inside
the uenv shell. Submit SLURM jobs from a **plain** login shell instead (with the
`#SBATCH --uenv` / `--view` header) — `sbatch` is blocked inside a uenv session.

## 1. Build integration `.so` + wrapper

GH200:
```bash
export GENCODE_ARCH="arch=compute_90,code=sm_90"
```

One `.so` per precision:
```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp64
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp32
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec fp16
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --integration --lower-all --lowprec bf16
# → libvelocity_gpu_stage8_solve_nh_integration_release.{fp64,fp32,fp16,bf16}.so
```

`bf16` and `fp16` share a wrapper: the type swap happens behind the
`dace::float16` typedef, so the flattened argument list and every symbol name
are identical and `gen_fortran_wrapper.py` emits a byte-identical `wrapper.f90`
for both. Swapping the `.so` under a built ICON needs no Fortran-side change.

The compile finds `sqlite3`, `zlib` and `libzstd` through `pkg-config` — the
activated `vt-gpu` env is what puts them there — and bakes their library
directories into the `.so` as RUNPATH, so the runtime loader resolves them
without `LD_LIBRARY_PATH`.

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

`--stage-dir` must point at the **`--integration` codegen that built the `.so`
you are integrating**. `codegen/stage8/<prec>/` is shared between build modes:
the integration build emits flattened scalar arguments, while the standalone
path (§4) overwrites the same directory with a struct-pointer interface
(`__dace_init(..., t_patch* p_patch, t_nh_prog* p_prog, ...)`). Both generate a
wrapper that compiles, but one built against the wrong mode mismatches the `.so`
at the call boundary. If a standalone build has run since, re-run the
`--integration` compile above before regenerating the wrapper.

> **✅ Artifacts ready**
>
> - `libvelocity_gpu_stage8_solve_nh_integration_release.{fp64,fp32,fp16,bf16}.so`
> - `wrapper.f90`
> - `serde.f90` (already in-tree; mostly auto-generated, small hand-written
>   API block — **TODO: fully auto-generate**)
>
> **Next: §2 hands off to icon-dace for data generation, §3 brings you
> back here for analysis.** §4 is the standalone NCU-profiling path,
> independent of §2–3 — skip unless regenerating the paper's NCU numbers.

## 2. Generate data (over in icon-dace)

Data generation — ICON build, submit, `.data` dumps — lives in
**icon-dace's `SC2026_HOWTO.md`**. In short: copy `wrapper.f90` into
`icon-dace/src/atm_dyn_iconam/`, build ICON, then run
`./run/sbatch_all_sc2026.sh <GRID>` over there for each grid.

Follow it through §8 of icon-dace's HOWTO, then come back here for
analysis.

## 3. Analysis — SNR (come back from icon-dace)

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
`OG_vs_F32`, `OG_vs_F16`, `OG_vs_BF16`, `ss5_vs_ss10`, `ss10_vs_F32`,
`ss10_vs_F16`, `ss10_vs_BF16`.

`PRECS` selects which lowered precisions to compare, defaulting to the three
`sbatch_all_sc2026.sh` submits. `BASELINE` picks the FP64 reference run —
`vanilla` by default, or `gpufp64` for a sweep that submitted no vanilla arm:

```bash
PRECS="fp32 fp16" BASELINE=gpufp64 ./run_snr_compare.sh 0010_R02B04
```

### Paper table (first-step SNR)

The paper reports SNR at the first physics generation — `phys=2` in our
data (phys=0/1 are init / pre-step). `report_serde.py --phys N` emits a
polars pivot per grid, rows = paper's 5 fields (`vn`, `w`, `vt`,
`vn_ie`, `w_concorr_c`), columns = tags:

```bash
python -m utils.report_serde snr.db --phys 2 --cross-only
# → PAPER SNR TABLE: grid=R02B04, phys=2
#   field       │ OG_vs_F32 │ OG_vs_F16 │ ss5_vs_ss10 │ ss10_vs_F32 │ ss10_vs_F16
#   vn          │ 154.10    │ 75.80     │ 66.60       │ 66.60       │ 66.10
#   ...
```

Column legend (REF → TEST):

| Tag             | REF                        | TEST                       | Interpretation                          |
|-----------------|----------------------------|----------------------------|-----------------------------------------|
| `OG_vs_F32`     | `ss5_vanilla` (FP64 ref)   | `ss5_gpufp32` (VT FP32)    | noise FP32 introduces                   |
| `OG_vs_F16`     | `ss5_vanilla` (FP64 ref)   | `ss5_gpufp16` (VT FP16)    | noise FP16 introduces                   |
| `OG_vs_BF16`    | `ss5_vanilla` (FP64 ref)   | `ss5_gpubf16` (VT BF16)    | noise BF16 introduces                   |
| `ss5_vs_ss10`   | `ss5_vanilla` (FP64 ref)   | `ss10_vanilla` (FP64 ref×2 substeps) | temporal-discretization floor |
| `ss10_vs_F32`   | `ss10_vanilla` (refined)   | `ss5_gpufp32` (VT FP32)    | FP32 vs the refined reference           |
| `ss10_vs_F16`   | `ss10_vanilla` (refined)   | `ss5_gpufp16` (VT FP16)    | FP16 vs the refined reference           |
| `ss10_vs_BF16`  | `ss10_vanilla` (refined)   | `ss5_gpubf16` (VT BF16)    | BF16 vs the refined reference           |

`ss5_vs_ss10` is the paper's SNR$_\text{time}$ column. Viability check (paper
convention): a reduced-precision mode must beat `ss5_vs_ss10` — otherwise its
noise has dipped below the temporal-discretization floor.

One pivot is emitted per grid in the DB.

## 4. Profile (standalone)

Only needed to regenerate the paper's NCU numbers. Skip for ICON
integration.

### 4.1 Reference data
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

### 4.2 Build standalone
```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp64
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp32
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp16
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec bf16
# → ./velocity_gpu_stage8_standalone_release.{fp64,fp32,fp16,bf16}
```

`bf16` retargets `dace::float16` to `__nv_bfloat16` through
`-DDACE_FLOAT16_IS_BFLOAT16`; the SDFG and every shape are identical to an
`fp16` build, since both types are two bytes. It trades three mantissa bits for
fp32's exponent range, which matters for the tendency outputs: `ddt_w_adv_pc`
underflows half's smallest normal and scores below 0 dB SNR in `fp16`, against
roughly 25 dB in `bf16`. Well-scaled intermediates such as `vt` and `vn_ie` go
the other way, ~74 dB down to ~56 dB. See `SC2026_HOWTO.ault.md` for the full
comparison.

Omitting `--integration` here overwrites `codegen/stage8/<prec>/` with the
standalone struct-pointer interface, which no longer describes any integration
`.so` built from that directory. Before returning to §1 — regenerating
`wrapper.f90` or rebuilding an integration `.so` — re-run the `--integration`
compile so the directory matches again.

### 4.3 Run standalone
```bash
./velocity_gpu_stage8_standalone_release.fp64 2 --reps=3 --data=data_r02b04
#   2         : timestep
#   --reps    : timing repetitions
#   --data    : reference-dump dir (defaults to data_nproma<NPROMA>)
```

### 4.4 NCU — submit

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

### 4.5 NCU — extract once, report many

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

### 4.6 Metric mapping
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
