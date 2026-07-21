# SC2026 — Velocity Tendencies (VT), ault

**Scope**: only **stage 8**. Performance/NCU runs on CSCS ault (A100 node
`ault25`). The **numerical** results (ICON integration, SNR) are produced on
daint (GH200) — see `SC2026_HOWTO.daint.md`. On ault only the **standalone
profiling path** (§4 of the daint HOWTO) is relevant; there is no ICON
integration here.

> ## Known limitations
> - **R02B06 does not run.** It reads its dumps and initialises the GPU (only
>   ~1.7 GB), then aborts with an uncaught C++ exception at the first timing
>   iteration. Not a memory ceiling — host RSS peaks at 12–19 GB and the GPU is
>   nearly idle. R02B03/04/05 are unaffected.
> - **NCU 2023.1.1** is what the spack env provides, older than the 2025.2 the
>   metric mapping in daint §4.6 was written against. Profiling and extraction
>   both work, but some metrics come back `n/a` (coalescing %, sectors/request,
>   warps launched). Re-check names before trusting those columns.

## Prerequisites

### Spack (one-time)

Spack and its packages repo live in two clones. Pick any location; the example
uses `$SCRATCH` (on ault this resolves to `/scratch/$USER` — plenty of space,
but expect it to be scrubbed after long inactivity; don't keep unique work
there).

```bash
export SPACK_TREE=$SCRATCH/spack-tree
mkdir -p $SPACK_TREE && cd $SPACK_TREE
git clone https://github.com/spack/spack.git
git -C spack checkout 0fe003d0
git clone https://github.com/spack/spack-packages.git
git -C spack-packages checkout 334aeef7

source $SPACK_TREE/spack/share/spack/setup-env.sh
spack repo remove builtin 2>/dev/null || true   # drop any stale user-scoped entry
spack repo add $SPACK_TREE/spack-packages/repos/spack_repo/builtin
```

Add the `source` + `spack repo add` lines to your shell rc. A site-wide spack
lives at `/apps/ault/spack/` if you prefer to reuse it; the recipe assumes a
fresh clone for reproducibility.

Both repos are pinned. An unpinned `HEAD` pair drifts off the `nvhpc` versions
the environments request — `26.1` for the ICON build (`icon-gpu`), `23.3` for
CLOUDSC — which surfaces at concretize time as `No version exists that satisfies
nvhpc@...`.

### Get the code (one-time)

Sparse-checkout `velocity/`, keeping the clone so later updates are a `git pull`
rather than a re-clone:

```bash
git clone --depth 1 --filter=blob:none --sparse \
  --branch okbuddyicon https://github.com/pratyai/icon-artifacts.git icon-vt-dace
cd icon-vt-dace
git sparse-checkout set velocity
cd velocity
```

All subsequent commands assume you are in `icon-vt-dace/velocity`.

### VT spack env (one-time)

No uenv on ault, so the daint `spack.yaml` (uenv `/user-environment` externals)
does not apply. `arch/cscs/ault/spack.yaml` has spack provide everything —
`cuda@12.1.1`, `gcc@12`, `sqlite`, `zlib`, `zstd` — with only a bootstrap gcc
declared external. VT builds with nvcc + g++, so it needs no nvhpc/hdf5/cmake.

Both version floors are hard requirements, not preferences:

- **cuda ≥ 12** — the standalone compiles with `-std=c++20`, and nvcc only
  accepts c++20 from 12.0 (11.x fails with `Value 'c++20' is not defined for
  option 'std'`).
- **gcc ≥ 10** — the serde headers `#include <ranges>`; older libstdc++ has no
  such header (`fatal error: ranges: No such file or directory`).

```bash
spack env create vt-gpu ./arch/cscs/ault/spack.yaml
spack -e vt-gpu concretize
spack -e vt-gpu install        # gcc@12 from source is the slow part
spack env activate vt-gpu
```

Activating the env is what puts nvcc on `PATH` and `sqlite3`/`zlib`/`libzstd` on
`pkg-config`'s path — the build fails on both without it, and the pkg-config
hits are what get baked into the binary as RUNPATH.

### Python venv (one-time)

Ault's system `python3` is 3.6.8, too old, and login-only. Use uv-managed 3.12
so the interpreter is visible on `ault25`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"            # add to shell rc too

uv python install 3.12
uv venv --python 3.12 --python-preference only-managed .venv
source .venv/bin/activate
uv pip install numpy h5py polars scipy netCDF4 tqdm zstandard
```

DaCe must be installed editable from a `--recursive` clone (a wheel drops
subpackages without `__init__.py`; the submodules carry `cub`/`rtllib`/… that
the codegen imports):

```bash
git clone --recursive -b f2dace/staging https://github.com/spcl/dace.git ../../dace
uv pip install -e ../../dace
```

`ncu` comes from the spack env (2023.1.1); §4 needs the env active.

## Every login

```bash
export SPACK_TREE=$SCRATCH/spack-tree
export PATH="$HOME/.local/bin:$PATH"
source $SPACK_TREE/spack/share/spack/setup-env.sh
spack env activate vt-gpu
source .venv/bin/activate            # from icon-vt-dace/velocity
```

No uenv, so that covers everything.

## 1. Build standalone

A100:
```bash
export GENCODE_ARCH="arch=compute_80,code=sm_80"
```

```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp64
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp32
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp16
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec bf16
# → ./velocity_gpu_stage8_standalone_release.{fp64,fp32,fp16,bf16}
```

These are the standalone builds (perf/NCU). For the integration `.so` + wrapper
used by the SNR path, run daint `SC2026_HOWTO.daint.md` §1 verbatim — same
`--integration` compiles and `gen_fortran_wrapper.py` — but keep the `sm_80`
`GENCODE_ARCH` above. Building ICON against those `.so` files and running the
integration is covered in icon-dace `SC2026_HOWTO.md`, *Running on ault (A100)*.

### bfloat16

`bf16` reuses the `float16` data descriptor — same two bytes, so the SDFG,
every shape and stride, and the Python side are identical to an `fp16` build.
Only the C++ typedef behind `dace::float16` changes, via
`-DDACE_FLOAT16_IS_BFLOAT16`, which retargets it to `__nv_bfloat16`. nvcc then
emits native `HFMA2.BF16_V2` for the lowered arithmetic.

Two consequences worth knowing before reading results:

- **Relational operators promote to fp32.** `cuda_bf16.hpp` has no packed bf16
  compare on sm_80, so comparisons cost extra `FSETP`. Against an fp16 build the
  whole binary runs about 7% more instructions with byte-identical memory
  traffic, so bf16 is not a throughput play — it buys exponent range.
- **`abs` and the mixed `float16`/`double` operators come from
  `include/fp16_operators.h`**, injected into generated `.cu` files. `bf16`
  needs it more than `fp16` does: `__nv_bfloat16` converts implicitly from
  `double`, so without the exact overloads an expression like `float16 * double`
  is ambiguous between the built-in arithmetic path and `cuda_bf16.hpp`'s
  operator.

Accuracy splits by whether a field is range-limited or precision-limited.
Against reference dumps at timestep 2, SNR in dB:

| field | fp32 | fp16 | bf16 |
|---|---|---|---|
| `p_diag % ddt_w_adv_pc` | 121.5 | **−0.8** | **24.3** |
| `p_diag % ddt_vn_apc_pc` | 146.6 | 50.4 | 51.8 |
| `p_diag % vt` | 152.4 | 74.1 | 56.1 |
| `p_metrics % ddqz_z_full_e` | 152.5 | 74.2 | 54.7 |

The tendency outputs underflow half's smallest normal (~6e-5), which is why
fp16 scores below 0 dB on `ddt_w_adv_pc` — error exceeding signal — while bf16
carries fp32's exponent range and keeps it. Well-scaled intermediates go the
other way by roughly 18 dB, which is precisely the three mantissa bits bf16
gives up (11 → 8, ~6 dB per bit).

## 2. Reference data

Same tarballs as daint §4.1 — polybox folder `SC2026 Data Dumps`, timestep 2
for R02B03/04/05, 1 for R02B06:

Extracted sizes grow ~4× per refinement: R02B03 780 MB, R02B04 3.1 GB,
R02B05 13 GB, R02B06 40 GB.

```bash
SHARE='https://polybox.ethz.ch/index.php/s/xfprBf6rYjY7EZD/download'
for F in data_r02b03.tar.zst data_r02b04.tar.zst data_r02b05.tar.zst data_r02b06.tar.zst; do
  curl -L -o "$F" "$SHARE?files=$F"
  tar -I zstd -xf "$F"
done
# → data_r02b0{3,4,5,6}/
```

## 3. Run standalone

The GPU is not the constraint — even the largest grid peaks around 1.7 GB of
the A100's memory. Host RAM is: reading the reference dumps needs far more than
SLURM's 8 GB default, which otherwise oom-kills the job mid-read. Request
memory to match the grid (R02B05 peaks ~12 GB, R02B06 ~19 GB).

```bash
srun --partition=amda100 --nodelist=ault25 --gres=gpu:a100:1 --mem=64G \
  ./velocity_gpu_stage8_standalone_release.fp64 2 --reps=10 --data=data_r02b04
#   2       : timestep (1 for R02B06)   --reps : timing repetitions
#   --data  : reference-dump dir
```

Each rep prints `Timer … took N us`; the first is cold (JIT) and should be
dropped before taking a median.

## 4. NCU — profile, extract, report

`profile_ncu.sh` ships a daint SBATCH header. Rather than fork it, override the
directives with `sbatch` CLI flags (they take precedence over `#SBATCH`).
**Submit from a shell with the spack env activated** — the script sets up no
environment of its own, so it inherits `PATH` from the submitter and otherwise
dies instantly with `ncu: command not found`:

```bash
spack env activate vt-gpu
sbatch --partition=amda100 --nodelist=ault25 --gres=gpu:a100:1 --time=04:00:00 \
  profile_ncu.sh fp64 2 data_r02b04 vt_profiles-r02b04
```

`amda100` caps at 4 h, which `--set full` on the larger grids can approach.

The sweep is otherwise identical to daint §4.4 — the precisions × the grids you
keep. Extract and report exactly as daint §4.5–4.6:

```bash
python extract_ncu.py vt_profiles-r02b0*/vt.*.ncu-rep   # one-shot → 2 CSVs
python report_ncu.py                                    # aggregate + dominant kernel
```

The metric mapping (daint §4.6) is NCU-version-specific; see the note on
2023.1.1 at the top of this file.

## Key differences from daint

| Aspect | daint | ault |
|---|---|---|
| GPU | GH200 (cc90) | A100 (cc80) |
| GENCODE | `compute_90,code=sm_90` | `compute_80,code=sm_80` |
| Toolchain | uenv `icon/25.2` (cuda 12.6) | spack `cuda@12.1.1` + `gcc@12`, no uenv |
| spack env | `arch/cscs/daint/spack.yaml` (uenv externals) | `arch/cscs/ault/spack.yaml` |
| `$SCRATCH` | `/capstor/scratch/cscs/$USER` | `/scratch/$USER` |
| Node runtime | `--uenv=… --view=default` | none |
| SLURM | `-p debug/normal`, `--uenv` header | `-p amda100 --nodelist=ault25 --gres=gpu:a100:1`, 4 h cap |
| Workflow | integration (numerical) + standalone (perf) | both — standalone/NCU here, integration in icon-dace |
| Integration/SNR | yes | yes — icon-dace `SC2026_HOWTO.md`, *Running on ault (A100)* |
