#!/bin/bash
# Run all 5 SNR comparisons for one grid. Writes into a shared SQLite DB
# keyed by (grid, tag, phys, field, sub_field).
#
# Usage:
#   ./run_snr_compare.sh <GRID> [EXP_DIR] [DB]
#     GRID    : <gridID>_<refinement>, e.g. 0010_R02B04
#     EXP_DIR : ICON experiments dir
#               (default: ../icon-dace/build/verification/experiments)
#     DB      : shared SQLite output
#               (default: ./snr.db)
#
# Run from the VT tree (icon-vt-dace), so `python -m utils.compare_serde`
# resolves.
set -euo pipefail

GRID=${1:? "Usage: $0 <GRID> [EXP_DIR] [DB]  (e.g. 0010_R02B04)"}
EXP=${2:-../icon-dace/build/verification/experiments}
DB=${3:-./snr.db}

GRID_LABEL="${GRID##*_}"   # 0010_R02B04 -> R02B04

V5="$EXP/sc2026_dt8_ss5_vanilla_$GRID"
V10="$EXP/sc2026_dt8_ss10_vanilla_$GRID"
F32="$EXP/sc2026_dt8_ss5_gpufp32_$GRID"
F16="$EXP/sc2026_dt8_ss5_gpufp16_$GRID"

cmp() {
    python -m utils.compare_serde -o "$DB" cross "$1" "$2" --grid "$GRID_LABEL" --tag "$3"
}

cmp "$V5"  "$F32" FP32_vs_FP64
cmp "$V5"  "$F16" FP16_vs_FP64
cmp "$V5"  "$V10" FP64_vs_refined
cmp "$V10" "$F32" FP32_vs_refined
cmp "$V10" "$F16" FP16_vs_refined
