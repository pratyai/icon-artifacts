# ICON ndyn_substeps Convergence Experiment

## Setup

- **Physics timestep**: dt = 8s
- **Grids**: R02B03 (g0050), R02B04 (g0010), R02B05 (g0008)
- **Test case**: exclaim_ape (aquaplanet)
- **Substep counts**: ss = 5, 6, 7, 8, 9, 10 (i.e. dynamics dt = 8/5, 8/6, ..., 8/10 seconds)

## Protocol

1. Run ICON normally for 19 physics iterations (p=1..19) with default ndyn_substeps=5
2. At p=20, override ndyn_substeps to the target value and enable serialization
3. Dump state at the entry of each physics iteration (first substep, first VT predictor call) from p=20 through p=70
4. Repeat for each substep count (5..10), all starting from the identical state at p=19

## Data files

Each directory contains serialized Fortran data in text format (zstd-compressed):

```
<field>.t0.p<phys>.d1.vt1.ss<substeps>.data.zst
```

- `t0`: time level 0 (iteration entry state)
- `p<N>`: physics iteration number
- `d1.vt1`: first dynamics substep, first velocity_tendencies call (predictor)
- `ss<N>`: ndyn_substeps value for this run

### Serialized fields

| Field | Type | Description |
|-------|------|-------------|
| p_prog | struct | Prognostic state: vn (normal velocity), w (vertical velocity) |
| p_diag | struct | Diagnostic fields (tendencies, fluxes) |
| p_metrics | struct | Grid metrics (time-invariant, should be identical across runs) |
| p_patch | struct | Grid topology (skipped from comparison) |
| p_int | struct | Interpolation coefficients (skipped from comparison) |
| z_kin_hor_e | array | Horizontal kinetic energy on edges |
| z_vt_ie | array | Tangential velocity at half levels |
| z_w_concorr_me | array | Contravariant correction for w |

## Analysis

```bash
# Pairwise comparison across all substep counts, stored in SQLite
python compare_got_and_want_v3.py <dir> -o results.db

# Summary report
python reporter_v3.py results.db
python reporter_v3.py results.db --per-field
```
