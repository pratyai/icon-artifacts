#!/bin/bash
#SBATCH --job-name=cloudsc-ncu
#SBATCH --account=g34
#SBATCH --time=02:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --partition=normal
#SBATCH --gres=gpu:1

# Usage: sbatch profile_ncu.sh <precision> <steps> [output_dir] [--reps=N] [extra ncu/binary args...]
# Example: sbatch profile_ncu.sh f32 2 build/profiles
#          sbatch profile_ncu.sh f64 1 build/profiles --reps=1

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: sbatch $0 <precision: f16|f32|f64> <steps> [output_dir] [--reps=N] [extra args...]"
    exit 1
fi

PREC="$1"
STEPS="$2"
shift 2

# Optional output dir: if $1 doesn't start with '-', treat as outdir.
OUTDIR="build/profiles"
if [ -n "$1" ] && [ "${1:0:1}" != "-" ]; then
    OUTDIR="$1"
    shift
fi

# Collect remaining args (--reps=N, or anything else to forward to the binary).
# Default --reps=3 if the user didn't supply one.
REPS_FLAG="--reps=3"
EXTRA_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --reps=*) REPS_FLAG="$arg" ;;
        *)        EXTRA_ARGS+=("$arg") ;;
    esac
done

BIN="./build/bin/cloudsc_gpu_bin.${PREC}"
if [ ! -x "$BIN" ]; then
    echo "Error: $BIN not found or not executable."
    echo "Build it first: python cloudsc_gpu_pipeline.py --lowprec fp${PREC#f} && ./recompile.gpu.${PREC}.sh"
    exit 1
fi

mkdir -p "$OUTDIR"

ulimit -s unlimited

# --set full alone (no --metrics). NCU 2025.2.0.0 treats explicit --metrics
# as a *replacement* for the set's counters, dropping raw .sum values. Raw
# FLOP instruction counts (smsp__sass_thread_inst_executed_op_*_pred_on.sum)
# can be reconstructed from .sum.per_cycle_elapsed x cycles_elapsed.avg,
# which --set full does collect.  See velocity/extract_ncu.py for reconstruction.
ncu --set full --import-source yes \
    -o "${OUTDIR}/cloudsc.${PREC}" -f \
    "$BIN" "$STEPS" "$REPS_FLAG" "${EXTRA_ARGS[@]}"
