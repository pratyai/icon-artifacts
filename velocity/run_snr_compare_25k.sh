#!/bin/bash
# 25k GPU-only SNR db: FP64 baseline = gpufp64 (no vanilla runs exist).
set -euo pipefail
GRID=${1:-0010_R02B04}
EXP=${2:-../icon-dace/build/verification/experiments}
DB=${3:-./snr_25k.db}
PY=${PY:-./.venv/bin/python}
GRID_LABEL="${GRID##*_}"

F64_5="$EXP/sc2026_dt8_ss5_gpufp64_$GRID"
F64_10="$EXP/sc2026_dt8_ss10_gpufp64_$GRID"
F32="$EXP/sc2026_dt8_ss5_gpufp32_$GRID"
F16="$EXP/sc2026_dt8_ss5_gpufp16_$GRID"
BF16="$EXP/sc2026_dt8_ss5_gpubf16_$GRID"

cmp() { "$PY" utils/compare_serde.py -o "$DB" cross "$1" "$2" --grid "$GRID_LABEL" --tag "$3"; }

cmp "$F64_5"  "$F32"    FP32_vs_FP64
cmp "$F64_5"  "$F16"    FP16_vs_FP64
cmp "$F64_5"  "$BF16"   BF16_vs_FP64
cmp "$F64_5"  "$F64_10" FP64_vs_refined
cmp "$F64_10" "$F32"    FP32_vs_refined
cmp "$F64_10" "$F16"    FP16_vs_refined
cmp "$F64_10" "$BF16"   BF16_vs_refined
echo "SNR_25K_DONE"
