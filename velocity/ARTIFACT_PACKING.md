# SC2026 — packing the Zenodo artifacts

Recipes for building the tarballs published on Zenodo (record 19708601). Each
one is a source tree with its VCS metadata stripped, so a reviewer can unpack
and build without needing git history or network access to a forge.

This file lives in `velocity/`, which the VT tarball carries, so the recipes
travel with the artifacts they describe. Each `cd` below is relative to the
checkout that holds the named tree, not to this file.

## Conventions

Every tarball unpacks into a single top-level directory named after the tree it
carries (`dace/`, `icon-vt-dace/`, `icon-dace/`), so extraction never scatters
files into the current directory.

Excluded from all of them:

- `.git/` — reviewers get a snapshot, not a repository. Keeping it would also
  leak branch names and remotes that mean nothing outside the authors' accounts.
- Editor, OS and Python scratch: `.DS_Store`, `._*` (macOS AppleDouble),
  `__pycache__/`, `*.pyc`, `.venv/`, `*.egg-info/`.
- SQLite sidecars: `*.db-wal`, `*.db-shm`. They are transient write-ahead-log
  files; shipping one next to its `.db` can leave the database inconsistent when
  a reviewer opens it. Only A1-vt ships databases, so the exclusion lives there.
- Products the HOWTOs regenerate: `codegen/`, `ptx_out/`, `*.o`, `*.so`.

Do not exclude `build/` by name. Upstream trees ship directories with that name
holding real sources — `dace/external/moodycamel/build/` is makefiles, not
output — so the exclusion is written per-tarball against the paths that
actually accumulate products.

Submodule contents **are** included. They are ordinary directories once `.git`
is gone, and a reviewer cannot run `git submodule update` against a snapshot.

Pack on Linux where possible. macOS `tar` writes AppleDouble `._*` companions
and `LIBARCHIVE.xattr.*` headers that GNU `tar` then warns about on every entry;
the `--no-xattrs --no-mac-metadata` flags below suppress them when packing on
macOS is unavoidable.

## A1-dace-sc2026.tar.gz

DaCe with the bfloat16 retarget. Everything the VT build imports, including the
`external/` submodules the codegen reaches into.

```bash
cd <parent-of-dace-checkout>          # the tarball's top dir is `dace/`

# Submodules must be populated: the codegen imports dace.external.rtllib, and
# a snapshot cannot fetch them later.
( cd dace && git submodule update --init --recursive )

tar --no-xattrs --no-mac-metadata \
    --exclude-vcs \
    --exclude='.DS_Store' --exclude='._*' \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.venv' --exclude='*.egg-info' \
    -czf A1-dace-sc2026.tar.gz dace/
```

`dace/viewer/webclient/dist/` carries the prebuilt SDFG-viewer bundle, roughly
20 MB of minified JavaScript and fonts. Nothing in the HOWTOs opens the viewer,
so its presence only changes the download size.

Verify before upload:

```bash
tar -tzf A1-dace-sc2026.tar.gz | grep -c '/\.git/'          # 0
tar -tzf A1-dace-sc2026.tar.gz | grep -c '^\._\|/\._'       # 0
tar -tzf A1-dace-sc2026.tar.gz | grep 'dace/external/cub/'  # non-empty
tar -tzf A1-dace-sc2026.tar.gz | grep 'runtime/include/dace/types.h'
```

The last check matters for this revision: `types.h` carries the
`DACE_FLOAT16_IS_BFLOAT16` guard that retargets `dace::float16` to
`__nv_bfloat16`, which is what the `--lowprec bf16` build defines.

## A1-icondace-sc2026.tar.gz

ICON with the VT dispatch wrapper and the SC2026 run scaffolding.

The `externals/` trees are vendored directly — this repo has no `.gitmodules`,
so no submodule step is needed.

`build/verification/` is packed, but only the curated set the repo already
tracks: the `LOG.SAVEME-*` run logs that Figure 7(a)'s timings are extracted
from, the `dt8_g*.db` substep-convergence databases, and the comparison
helpers. Compiled output and `experiments/` dumps stay out — a reviewer
regenerates those, and the dumps run to tens of gigabytes. `build/config.log`
records the configure invocation from the packer's own machine, so it is
dropped as well.

```bash
cd <parent-of-icon-dace-checkout>     # the tarball's top dir is `icon-dace/`

tar --no-xattrs --no-mac-metadata \
    --exclude-vcs \
    --exclude='.DS_Store' --exclude='._*' \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.venv' --exclude='*.egg-info' \
    --exclude='icon-dace/build/config.log' \
    -czf A1-icondace-sc2026.tar.gz icon-dace/
```

Verify before upload:

```bash
tar -tzf A1-icondace-sc2026.tar.gz | grep -c '/\.git/'                    # 0
tar -tzf A1-icondace-sc2026.tar.gz | grep -oE '^icon-dace/externals/[^/]+' | sort -u | wc -l   # 16
tar -tzf A1-icondace-sc2026.tar.gz | grep -c 'LOG.SAVEME'                 # 16
tar -xzOf A1-icondace-sc2026.tar.gz icon-dace/run/sbatch_all_sc2026.sh | grep -c bf16   # 1
```

The last two checks pin this revision: the `LOG.SAVEME` logs must travel for
Figure 7(a) to be reproducible from the artifact alone, and `sbatch_all_sc2026.sh`
must carry the §8.6 BF16 arm.

## A1-vt-sc2026.tar.gz

The VT tree: precision-lowering pipeline, HOWTOs, generated code, and the SNR
database behind Table II.

The tarball's top directory is `icon-vt-dace/velocity/`, matching the layout the
HOWTO produces — it sparse-checks-out `velocity/` from the artifacts repo into
`icon-vt-dace/`, so a reviewer's tree and the tarball agree path for path.
`-s` rewrites the prefix at pack time, since the source checkout has no
`icon-vt-dace/` level of its own.

`codegen/` and `ptx_out/` travel: they are what the lowering pipeline emits, and
shipping them lets a reviewer read the generated CUDA and PTX without first
reproducing a build. Compiled output does not — `*.so`, the
`velocity_gpu_stage8_standalone_release.*` binaries and `shared_libs/` are
architecture-specific and rebuilt in §1. Reference dumps (`data_r02b0*/`) and
comparison output (`gotwant/`) stay out as well; they are downloaded or
regenerated, and run to tens of gigabytes.

```bash
cd <ico2-checkout>                    # velocity/ lives here

tar --no-xattrs --no-mac-metadata \
    -s '|^velocity|icon-vt-dace/velocity|' \
    --exclude-vcs \
    --exclude='.DS_Store' --exclude='._*' \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.venv' --exclude='*.egg-info' \
    --exclude='*.so' \
    --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='velocity_gpu_stage8_standalone_release.*' \
    --exclude='shared_libs' \
    --exclude='data_r02b0*' --exclude='gotwant' \
    --exclude='dummy_shapes.py' --exclude='extract_serde_shapes.py' \
    --exclude='run_velocity_sdfg.py' \
    --exclude='wrapper.f90' --exclude='recompile.*.sh' \
    -czf A1-vt-sc2026.tar.gz velocity/
```

`tar` packs a directory as it finds it, so untracked working files ship unless
named. The last two exclusion lines cover those: three local-only helper scripts
that no pipeline step invokes, plus the per-precision `wrapper.f90` and
`recompile.<mode>.<prec>.sh` that each build regenerates. The tracked
`velocity_gpu_wrapper.f90` and `recompile.sh` are different files and do travel.

GNU `tar` spells the prefix rewrite `--transform='s|^velocity|icon-vt-dace/velocity|'`.

Verify before upload:

```bash
tar -tzf A1-vt-sc2026.tar.gz | awk -F/ '{print $1}' | sort -u        # icon-vt-dace
tar -tzf A1-vt-sc2026.tar.gz | grep -c '\.so$'                       # 0
tar -tzf A1-vt-sc2026.tar.gz | grep -c 'standalone_release\.'         # 0
tar -tzf A1-vt-sc2026.tar.gz | grep -c 'snr_bf16.db'                  # 1
tar -tzf A1-vt-sc2026.tar.gz | grep -cE '\.db-(wal|shm)$'             # 0
```

`shared_struct_defs.h` is generated from the committed `shared_struct_defs.h.in`
template by `utils/generate_storage_types.py`, and its field types follow
whichever precision the generating build ran. Whatever it holds at pack time is
overwritten on the reviewer's first compile.
