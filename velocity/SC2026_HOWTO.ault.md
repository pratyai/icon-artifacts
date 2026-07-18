# SC2026 — Velocity Tendencies (VT), ault

**Scope**: only **stage 8**. Performance/NCU runs on CSCS ault (A100 node
`ault25`). The **numerical** results (ICON integration, SNR) are produced on
daint (GH200) — see `SC2026_HOWTO.daint.md`. On ault only the **standalone
profiling path** (§4 of the daint HOWTO) is relevant; there is no ICON
integration here.

> ## ⚠️ Unverified on ault — confirm on a real run, then delete this box
> Nothing below has been exercised on ault for VT yet (the daint HOWTO has;
> the old `shared_libs/ault*` builds predate this workflow). The items marked
> **⚠️** are best-guess, adapted from the cloudsc ault HOWTO and the A100
> target:
> 1. **`arch/cscs/ault/spack.yaml`** is drafted (cuda + gcc + sqlite/zlib/zstd,
>    modeled on the cloudsc ault 21.3 env) but its **cuda external
>    (`11.2.142` at `/opt/nvidia/hpc_sdk/.../21.3/cuda`) is unverified** — the
>    path and whether 11.2's nvcc/CUB build the sm_80 kernels (daint uses 12.6).
> 2. **GENCODE** `arch=compute_80,code=sm_80` (A100 cc80) — standard, unverified
>    against this build.
> 3. **`profile_ncu.sh` has a daint SBATCH header** (`--partition=normal
>    --gres=gpu:1`); §4 overrides it with `sbatch` CLI flags (which win over
>    `#SBATCH`) rather than forking the script — confirm the flags are right for
>    ault (`--partition=total --nodelist=ault25 --gres=gpu:a100:1`).
> 4. **Whether the standalone builds/runs on A100** with the ault toolchain
>    (FP16 in particular may ICE on an older nvhpc, as it does for cloudsc 21.3).
> 5. **`ncu` availability/version** on ault (the daint metric mapping assumes
>    NCU 2025.2).

## Prerequisites

### Spack (one-time)

Spack and its packages repo live in two clones. Pick any location; the example
uses `$SCRATCH` (on ault this resolves to `/scratch/$USER` — plenty of space,
but expect it to be scrubbed after long inactivity; don't keep unique work
there).

```bash
export SPACK_TREE=$SCRATCH/spack-tree
mkdir -p $SPACK_TREE && cd $SPACK_TREE
git clone --depth=1 https://github.com/spack/spack.git
git clone --depth=1 https://github.com/spack/spack-packages.git

source $SPACK_TREE/spack/share/spack/setup-env.sh
spack repo remove builtin 2>/dev/null || true   # drop any stale user-scoped entry
spack repo add $SPACK_TREE/spack-packages/repos/spack_repo/builtin
```

Add the `source` + `spack repo add` lines to your shell rc. A site-wide spack
lives at `/apps/ault/spack/` if you prefer to reuse it; the recipe assumes a
fresh clone for reproducibility.

### Get the code (one-time)

Sparse-checkout `velocity/`, keeping the clone so later updates are a `git pull`
rather than a re-clone:

```bash
git clone --depth 1 --filter=blob:none --sparse \
  --branch okbuddyicon git@github.com:pratyai/icon-artifacts.git icon-vt-dace
cd icon-vt-dace
git sparse-checkout set velocity
cd velocity
```

All subsequent commands assume you are in `icon-vt-dace/velocity`.

### VT spack env (one-time)

No uenv on ault, so the daint `spack.yaml` (uenv `/user-environment` externals)
does not apply. `arch/cscs/ault/spack.yaml` instead declares `cuda` + `gcc` as
externals (from the site NVHPC 21.3 install, the same prefix the cloudsc ault
env uses) plus `sqlite`/`zlib`/`zstd`. VT builds with nvcc + g++, so it needs no
nvhpc/hdf5/cmake.

```bash
spack env create vt-gpu ./arch/cscs/ault/spack.yaml
spack -e vt-gpu concretize
spack -e vt-gpu install
spack env activate vt-gpu
```

⚠️ Verify the cuda external (`11.2.142` prefix) resolves on ault, and that its
nvcc/CUB build the sm_80 kernels (daint uses 12.6). If not, repoint `cuda:` at a
newer site cuda or add `cuda@12` to the specs to spack-build it.

Activating the env is what puts `sqlite3`/`zlib`/`libzstd` on `pkg-config`'s
path for the build (§1), which bakes their dirs into the binary as RUNPATH.

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

`ncu` is needed for §4 — ⚠️ confirm it is installed on ault.

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
export GENCODE_ARCH="arch=compute_80,code=sm_80"    # ⚠️ cc80
```

```bash
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp64
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp32
python -m utils.stages.compile_gpu_stage8 --optimize --compile --release --reduce-bitwidth --lower-all --lowprec fp16
# → ./velocity_gpu_stage8_standalone_release.{fp64,fp32,fp16}
```

No `--integration` (that path is daint-only). ⚠️ FP16 may ICE on an older ault
nvhpc — if so, drop the fp16 line and profile fp64/fp32 only.

## 2. Reference data

Same tarballs as daint §4.1 — polybox folder `SC2026 Data Dumps`, timestep 2
for R02B03/04/05, 1 for R02B06:

```bash
SHARE='https://polybox.ethz.ch/index.php/s/xfprBf6rYjY7EZD/download'
for F in data_r02b03.tar.zst data_r02b04.tar.zst data_r02b05.tar.zst data_r02b06.tar.zst; do
  curl -L -o "$F" "$SHARE?files=$F"
  tar -I zstd -xf "$F"
done
# → data_r02b0{3,4,5,6}/
```

## 3. Run standalone

```bash
./velocity_gpu_stage8_standalone_release.fp64 2 --reps=3 --data=data_r02b04
#   2       : timestep      --reps : timing repetitions      --data : reference-dump dir
```

## 4. NCU — profile, extract, report

`profile_ncu.sh` ships a daint SBATCH header. Rather than fork it, override the
directives with `sbatch` CLI flags (they take precedence over `#SBATCH`):

```bash
sbatch --partition=total --nodelist=ault25 --gres=gpu:a100:1 --account=g34 \
  profile_ncu.sh f64 2 data_r02b04 vt_profiles-r02b04    # ⚠️ confirm flags on ault
```

The sweep is otherwise identical to daint §4.4 — 3 precisions × the grids you
keep (⚠️ A100 has 40 GB; the largest grids may OOM — start with R02B03/04).
Extract and report exactly as daint §4.5–4.6:

```bash
python extract_ncu.py vt_profiles-r02b0*/vt.*.ncu-rep   # one-shot → 2 CSVs
python report_ncu.py                                    # aggregate + dominant kernel
```

The metric mapping (daint §4.6) is NCU-version-specific — re-check the names if
ault's `ncu` differs from 2025.2.

## Key differences from daint

| Aspect | daint | ault |
|---|---|---|
| GPU | GH200 (cc90) | A100 (cc80) |
| GENCODE | `compute_90,code=sm_90` | `compute_80,code=sm_80` ⚠️ |
| Toolchain | uenv `icon/25.2` (cuda 12.6) | spack nvhpc (no uenv) ⚠️ |
| spack env | `arch/cscs/daint/spack.yaml` (uenv externals) | `arch/cscs/ault/spack.yaml` ⚠️ (to create) |
| `$SCRATCH` | `/capstor/scratch/cscs/$USER` | `/scratch/$USER` |
| Node runtime | `--uenv=… --view=default` | none |
| SLURM | `-p debug/normal`, `--uenv` header | `-p total --nodelist=ault25 --gres=gpu:a100:1` ⚠️ |
| Workflow | integration (numerical) + standalone (perf) | **standalone/NCU only** |
| Integration/SNR | yes | no — use daint |
