#!/usr/bin/env bash
# Compare OG ICON (FP64) vs NU ICON (F32/F16) serialized data across all grids.
#
# Usage:  bash utils/run_cross_compare.sh
#   or:   bash utils/run_cross_compare.sh -j 16
#
# Outputs one .db per (grid, precision) combination into $OUT_DIR.

set -euo pipefail

OG_BASE="/capstor/scratch/cscs/pmazumde/SC2026-DATA/OG-ICON-NDYNSUBSTEPS-VARIATIONS"
NU_BASE="/capstor/scratch/cscs/pmazumde/SC2026-DATA/NU-ICON-NDYNSUBSTEPS_5-DATA"
OUT_DIR="${OUT_DIR:-${NU_BASE}/cross_results}"

GRIDS=(
    "exclaim_ape_R02B04_dt8_g0008_R02B05"
    "exclaim_ape_R02B04_dt8_g0010_R02B04"
    "exclaim_ape_R02B04_dt8_g0050_R02B03"
)
PRECS=("F16" "F32")

EXTRA_ARGS=("$@")  # pass-through (e.g. -j 16, --force)

mkdir -p "$OUT_DIR"

for grid in "${GRIDS[@]}"; do
    og_dir="${OG_BASE}/${grid}"
    if [[ ! -d "$og_dir" ]]; then
        echo "SKIP (ref not found): $og_dir"
        continue
    fi
    for prec in "${PRECS[@]}"; do
        nu_dir="${NU_BASE}/${grid}-SER-${prec}"
        if [[ ! -d "$nu_dir" ]]; then
            echo "SKIP (test not found): $nu_dir"
            continue
        fi
        db="${OUT_DIR}/cross_${grid}_${prec}.db"
        echo "=== ${grid} OG vs ${prec} ==="
        python utils/compare_cross.py "$og_dir" "$nu_dir" -o "$db" "${EXTRA_ARGS[@]}"
        echo ""
    done
done

echo "All done. Results in: $OUT_DIR"
