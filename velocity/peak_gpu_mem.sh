#!/bin/sh
# Usage: ./peak_gpu_mem.sh [--gpu ID] [--interval MS] -- COMMAND [ARGS...]
#
# Measures peak GPU DRAM consumed by COMMAND by subtracting a baseline
# taken before the program starts.
#
# Example:
#   ./peak_gpu_mem.sh -- ./velocity_gpu_stage8_standalone_release.fp64 1
#   ./peak_gpu_mem.sh --gpu 2 --interval 50 -- ./my_program

set -e

gpu_id=0
interval_ms=100

while [ $# -gt 0 ]; do
    case "$1" in
        --gpu)      gpu_id="$2";       shift 2 ;;
        --interval) interval_ms="$2";  shift 2 ;;
        --)         shift; break ;;
        *)          break ;;
    esac
done

if [ $# -eq 0 ]; then
    echo "Usage: $0 [--gpu ID] [--interval MS] -- COMMAND [ARGS...]" >&2
    exit 1
fi

query="nvidia-smi --id=$gpu_id --query-gpu=memory.used --format=csv,noheader,nounits"

baseline=$($query | tr -d ' ')
echo "Baseline GPU memory: ${baseline} MiB"

logfile=$(mktemp /tmp/gpu_mem.XXXXXX)

nvidia-smi --id="$gpu_id" --query-gpu=memory.used \
    --format=csv,noheader,nounits -lms "$interval_ms" > "$logfile" &
monitor_pid=$!

"$@"
rc=$?

kill "$monitor_pid" 2>/dev/null || true
wait "$monitor_pid" 2>/dev/null || true

peak=$(sort -nr "$logfile" | head -n1 | tr -d ' ')
delta=$((peak - baseline))

echo "---"
echo "Peak GPU memory:     ${peak} MiB"
echo "Baseline:            ${baseline} MiB"
echo "Program used:        ${delta} MiB"

rm -f "$logfile"
exit $rc
