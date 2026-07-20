#!/bin/bash
# Run the SNR comparisons for one grid. Writes into a shared SQLite DB
# keyed by (grid, tag, phys, field, sub_field).
#
# Usage:
#   [BASELINE=vanilla|gpufp64] [PRECS="fp32 fp16 bf16"] \
#     ./run_snr_compare.sh <GRID> [EXP_DIR] [DB]
#     GRID     : <gridID>_<refinement>, e.g. 0010_R02B04
#     EXP_DIR  : ICON experiments dir
#                (default: ../icon-dace/build/verification/experiments)
#     DB       : shared SQLite output
#                (default: ./snr.db)
#     BASELINE : which FP64 run is the reference (default: vanilla).
#                `gpufp64` compares against the VT FP64 run instead, for
#                sweeps that submitted no vanilla arm.
#     PRECS    : lowered precisions to compare (default: fp32 fp16 bf16,
#                matching the arms sbatch_all_sc2026.sh submits).
#
# Each precision yields a `<PREC>_vs_FP64` tag, plus `<PREC>_vs_refined` when
# the ss10 reference exists; `FP64_vs_refined` is the temporal noise floor
# (SNR_time in the paper's Table II). Missing runs are skipped with a notice
# rather than aborting the sweep.
#
# Run from the VT tree (icon-vt-dace), so `python -m utils.compare_serde`
# resolves.
set -euo pipefail

GRID=${1:? "Usage: $0 <GRID> [EXP_DIR] [DB]  (e.g. 0010_R02B04)"}
EXP=${2:-../icon-dace/build/verification/experiments}
DB=${3:-./snr.db}
BASELINE=${BASELINE:-vanilla}
PRECS=${PRECS:-"fp32 fp16 bf16"}

GRID_LABEL="${GRID##*_}"   # 0010_R02B04 -> R02B04

V5="$EXP/sc2026_dt8_ss5_${BASELINE}_$GRID"
V10="$EXP/sc2026_dt8_ss10_${BASELINE}_$GRID"

[ -d "$V5" ] || { echo "missing baseline run: $V5" >&2; exit 1; }

cmp() {
    python -m utils.compare_serde -o "$DB" cross "$1" "$2" --grid "$GRID_LABEL" --tag "$3"
}

run_dir() { echo "$EXP/sc2026_dt8_ss5_gpu${1}_$GRID"; }

prec_tag() { echo "$1" | tr "[:lower:]" "[:upper:]"; }

for p in $PRECS; do
    RUN=$(run_dir "$p")
    if [ ! -d "$RUN" ]; then
        echo "skip ${p}: no run at $RUN" >&2
        continue
    fi
    cmp "$V5" "$RUN" "$(prec_tag "$p")_vs_FP64"
done

if [ ! -d "$V10" ]; then
    echo "skip refined comparisons: no ss10 run at $V10" >&2
    exit 0
fi

cmp "$V5" "$V10" FP64_vs_refined
for p in $PRECS; do
    RUN=$(run_dir "$p")
    [ -d "$RUN" ] || continue
    cmp "$V10" "$RUN" "$(prec_tag "$p")_vs_refined"
done
