#!/usr/bin/env bash
# build.sh — Run stage8 pipeline with sensible defaults.
# All stdout+stderr are teed to a timestamped log file.
#
# Usage:
#   ./build.sh                          # fp32, integration, optimize+compile
#   ./build.sh --lowprec fp16           # fp16
#   ./build.sh --lowprec fp64           # fp64 (no lowering)
#   ./build.sh --compile                # compile only (skip optimize)
#   ./build.sh --optimize               # optimize only (skip compile)
#   ./build.sh --no-integration         # standalone build
#   ./build.sh --no-lower-all           # only lower sensitivity candidates
#
# Extra env vars (optional):
#   SINGLE_THREADED=1  ./build.sh       # disable multiprocessing
#   _USE_CUDA_EVENTS=1 ./build.sh       # enable CUDA event timers
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
elif [ -f ../cloudsc/.venv/bin/activate ]; then
    source ../cloudsc/.venv/bin/activate
fi

# Defaults (can be overridden by passing CLI args through)
: "${_LOWPREC:=fp32}"
: "${_INTEGRATION:=1}"
: "${_RELEASE:=1}"
: "${_LOWER_ALL:=1}"
: "${_REDUCE_BITWIDTH:=1}"

export _LOWPREC _INTEGRATION _RELEASE _LOWER_ALL _REDUCE_BITWIDTH

# Log file
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOGFILE="build_${TIMESTAMP}.log"

# Build equivalent command
_CMD="python -m utils.stages.compile_gpu_stage8"
[ "$_RELEASE" = "1" ] && _CMD="$_CMD --release" || _CMD="$_CMD --no-release"
[ "$_INTEGRATION" = "1" ] && _CMD="$_CMD --integration" || _CMD="$_CMD --no-integration"
[ "$_LOWER_ALL" = "1" ] && _CMD="$_CMD --lower-all" || _CMD="$_CMD --no-lower-all"
[ "$_REDUCE_BITWIDTH" = "1" ] && _CMD="$_CMD --reduce-bitwidth" || _CMD="$_CMD --no-reduce-bitwidth"
_CMD="$_CMD --lowprec $_LOWPREC $*"

echo "=== build.sh | $(date) ==="
echo "  $_CMD"
echo "  log: $LOGFILE"
echo ""

# Run pipeline, tee to log
python -u -m utils.stages.compile_gpu_stage8 "$@" 2>&1 | tee "$LOGFILE"

echo ""
echo "=== Done | $(date) | log: $LOGFILE ==="
