#!/usr/bin/env bash
# Run spatial_diag.py for all consecutive grid pairs, OG data, cell fields.
# Usage: bash run_spatial_diag.sh [--nlev 90] [--phys 2]
#
# Outputs parent-child avg SNR for each (grid_pair, field) combination.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SPATIAL_DIAG="${SCRIPT_DIR}/spatial_diag.py"
PYTHON="${PYTHON:-python}"

NLEV="${NLEV:-90}"
PHYS="${PHYS:-2}"

EXP_DIR="${EXP_DIR:-experiments}"

# Grid files (coarse → fine)
declare -a GRID_FILES=(
    "icon_grid_0050_R02B03_G.nc"
    "icon_grid_0010_R02B04_G.nc"
    "icon_grid_0008_R02B05_G.nc"
    "icon_grid_0002_R02B06_G.nc"
)

# Experiment dir stems matching each grid
declare -a EXP_STEMS=(
    "exclaim_ape_R02B04_dt8_g0050_R02B03"
    "exclaim_ape_R02B04_dt8_g0010_R02B04"
    "exclaim_ape_R02B04_dt8_g0008_R02B05"
    "exclaim_ape_R02B04_dt8_g0002_R02B06"
)

# Cell fields: (serde_file_prefix, field_name)
CELL_FIELDS=(
    "p_prog:w"
    "p_diag:w_concorr_c"
    "p_diag:ddt_w_adv_pc"
)

# Edge fields (reconstructed to cells)
EDGE_FIELDS=(
    "p_prog:vn"
    "p_diag:vn_ie"
    "p_diag:vt"
    "p_diag:ddt_vn_apc_pc"
)

NGRID=${#GRID_FILES[@]}

echo "=== Spatial convergence diagnostics (OG, phys=${PHYS}, nlev=${NLEV}) ==="
echo ""

for (( i=0; i<NGRID-1; i++ )); do
    j=$((i+1))
    COARSE_GRID="${GRID_FILES[$i]}"
    FINE_GRID="${GRID_FILES[$j]}"
    COARSE_STEM="${EXP_STEMS[$i]}"
    FINE_STEM="${EXP_STEMS[$j]}"

    COARSE_LABEL="${COARSE_GRID##*_R0}"
    COARSE_LABEL="R0${COARSE_LABEL%%_*}"
    FINE_LABEL="${FINE_GRID##*_R0}"
    FINE_LABEL="R0${FINE_LABEL%%_*}"

    echo "====== ${COARSE_LABEL} → ${FINE_LABEL} ======"

    # Check dirs exist
    COARSE_DIR="${EXP_DIR}/${COARSE_STEM}-SER-OG"
    FINE_DIR="${EXP_DIR}/${FINE_STEM}-SER-OG"
    if [[ ! -d "$COARSE_DIR" ]]; then
        echo "  SKIP: $COARSE_DIR not found"
        continue
    fi
    if [[ ! -d "$FINE_DIR" ]]; then
        echo "  SKIP: $FINE_DIR not found"
        continue
    fi

    # Cell fields
    for entry in "${CELL_FIELDS[@]}"; do
        serde="${entry%%:*}"
        field="${entry##*:}"
        COARSE_FILE="${COARSE_DIR}/${serde}.t0.p${PHYS}.d1.vt1.ss5.data"
        FINE_FILE="${FINE_DIR}/${serde}.t0.p${PHYS}.d1.vt1.ss5.data"

        # Try .zst if plain doesn't exist
        [[ ! -f "$COARSE_FILE" ]] && COARSE_FILE="${COARSE_FILE}.zst"
        [[ ! -f "$FINE_FILE" ]] && FINE_FILE="${FINE_FILE}.zst"

        if [[ ! -f "$COARSE_FILE" || ! -f "$FINE_FILE" ]]; then
            echo "  SKIP ${field}: file not found"
            continue
        fi

        echo "  --- ${field} (${serde}) ---"
        $PYTHON "$SPATIAL_DIAG" \
            --coarse-grid "$COARSE_GRID" \
            --fine-grid "$FINE_GRID" \
            --coarse-file "$COARSE_FILE" \
            --fine-file "$FINE_FILE" \
            --field "$field" \
            --nlev "$NLEV" 2>&1 | grep -E "SNR|RMSE|Radial|stats"
        echo ""
    done

    # Edge fields
    for entry in "${EDGE_FIELDS[@]}"; do
        serde="${entry%%:*}"
        field="${entry##*:}"
        COARSE_FILE="${COARSE_DIR}/${serde}.t0.p${PHYS}.d1.vt1.ss5.data"
        FINE_FILE="${FINE_DIR}/${serde}.t0.p${PHYS}.d1.vt1.ss5.data"

        [[ ! -f "$COARSE_FILE" ]] && COARSE_FILE="${COARSE_FILE}.zst"
        [[ ! -f "$FINE_FILE" ]] && FINE_FILE="${FINE_FILE}.zst"

        if [[ ! -f "$COARSE_FILE" || ! -f "$FINE_FILE" ]]; then
            echo "  SKIP ${field}: file not found"
            continue
        fi

        echo "  --- ${field} (${serde}, edge→cell reconstruction) ---"
        $PYTHON "$SPATIAL_DIAG" \
            --coarse-grid "$COARSE_GRID" \
            --fine-grid "$FINE_GRID" \
            --coarse-file "$COARSE_FILE" \
            --fine-file "$FINE_FILE" \
            --field "$field" \
            --nlev "$NLEV" 2>&1 | grep -E "SNR|RMSE|Radial|stats"
        echo ""
    done
done

echo "=== Done ==="
