#!/usr/bin/env bash
#SBATCH --job-name=vt-serde-compare
#SBATCH --account=g34
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --time=04:00:00
#SBATCH --output=compare_%j.log
#
# Run all comparisons (OG convergence + OG vs NU cross) into a single DB.
#
# Usage:  bash utils/run_compare_all.sh
#   or:   bash utils/run_compare_all.sh -j 16
#   or:   bash utils/run_compare_all.sh --force -j 8
#
# Override paths:
#   OG_BASE=... NU_BASE=... DB=... bash utils/run_compare_all.sh

set -euo pipefail

OG_BASE="${OG_BASE:-/capstor/scratch/cscs/pmazumde/SC2026-DATA/OG-ICON-NDYNSUBSTEPS-VARIATIONS}"
NU_BASE="${NU_BASE:-/capstor/scratch/cscs/pmazumde/SC2026-DATA/NU-ICON-NDYNSUBSTEPS_5-DATA}"
DB="${DB:-${NU_BASE}/all_comparisons.db}"
SCRIPT="$(dirname "$0")/compare_serde.py"

EXTRA_ARGS=("$@")

# Grid dirs and short labels
declare -A GRIDS=(
    ["exclaim_ape_R02B04_dt8_g0050_R02B03"]="R02B03"
    ["exclaim_ape_R02B04_dt8_g0010_R02B04"]="R02B04"
    ["exclaim_ape_R02B04_dt8_g0008_R02B05"]="R02B05"
)

echo "DB: $DB"
echo ""

# 1. OG internal convergence (pairwise ss within each grid)
for dir_name in "${!GRIDS[@]}"; do
    grid="${GRIDS[$dir_name]}"
    og_dir="${OG_BASE}/${dir_name}"
    if [[ ! -d "$og_dir" ]]; then
        echo "SKIP convergence (not found): $og_dir"
        continue
    fi
    echo "=== Convergence: ${grid} ==="
    python "$SCRIPT" -o "$DB" "${EXTRA_ARGS[@]}" convergence "$og_dir" --grid "$grid"
    echo ""
done

# 2. Cross: OG vs NU at each (grid, precision)
PRECS=("F32" "F16")
for dir_name in "${!GRIDS[@]}"; do
    grid="${GRIDS[$dir_name]}"
    og_dir="${OG_BASE}/${dir_name}"
    if [[ ! -d "$og_dir" ]]; then
        echo "SKIP cross (ref not found): $og_dir"
        continue
    fi
    for prec in "${PRECS[@]}"; do
        nu_dir="${NU_BASE}/${dir_name}-SER-${prec}"
        if [[ ! -d "$nu_dir" ]]; then
            echo "SKIP cross (test not found): $nu_dir"
            continue
        fi
        tag="OG_vs_${prec}"
        echo "=== Cross: ${grid} ${tag} ==="
        python "$SCRIPT" -o "$DB" "${EXTRA_ARGS[@]}" cross "$og_dir" "$nu_dir" --grid "$grid" --tag "$tag"
        echo ""
    done
done

echo "All done. DB: $DB"
