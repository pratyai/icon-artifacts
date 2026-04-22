#!/bin/bash
#SBATCH --job-name=cloudsc-ncu
#SBATCH --account=g34
#SBATCH --time=02:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --partition=normal
#SBATCH --gres=gpu:1

# Usage: sbatch profile_ncu.sh <precision> <steps> [output_dir] [--reps=N] [--per-kernel=RANGE] [extra args...]
# Example: sbatch profile_ncu.sh f32 2 build/profiles
#          sbatch profile_ncu.sh f64 1 build/profiles --reps=1
#          sbatch profile_ncu.sh f64 1 --reps=1 --per-kernel=0-2   # first 3 invocations of each kernel only

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

# Collect remaining args. Recognize:
#   --reps=N              (binary arg)
#   --per-kernel=RANGE    (NCU: profile only RANGE invocations of each kernel,
#                          e.g. --per-kernel=0-2  or  --per-kernel=0)
# Anything else is forwarded to the binary as extra args.
REPS_FLAG="--reps=3"
PER_KERNEL_RANGE=""
EXTRA_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --reps=*)        REPS_FLAG="$arg" ;;
        --per-kernel=*)  PER_KERNEL_RANGE="${arg#--per-kernel=}" ;;
        *)               EXTRA_ARGS+=("$arg") ;;
    esac
done

NCU_KERNEL_FILTER=()
if [ -n "$PER_KERNEL_RANGE" ]; then
    # NCU --kernel-id wants `context:stream:[op:]name:invocation-NR` (single
    # int). To capture a range 0..N, emit one --kernel-id flag per invocation
    # index. Example --per-kernel=0-2 expands to --kernel-id repeated 3x.
    if [[ "$PER_KERNEL_RANGE" == *-* ]]; then
        LO="${PER_KERNEL_RANGE%-*}"
        HI="${PER_KERNEL_RANGE#*-}"
    else
        LO="$PER_KERNEL_RANGE"
        HI="$PER_KERNEL_RANGE"
    fi
    for i in $(seq "$LO" "$HI"); do
        NCU_KERNEL_FILTER+=(--kernel-id "::regex:.*:$i")
    done
    echo "[profile_ncu] profiling invocations ${LO}..${HI} of every kernel"
fi

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
    "${NCU_KERNEL_FILTER[@]}" \
    -o "${OUTDIR}/cloudsc.${PREC}" -f \
    "$BIN" "$STEPS" "$REPS_FLAG" "${EXTRA_ARGS[@]}"
