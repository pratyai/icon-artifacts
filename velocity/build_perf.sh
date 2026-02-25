#!/bin/bash
#SBATCH --job-name=velocity_build
#SBATCH --time=4:00:00

set -e

PRECISIONS=(fp16 fp32 fp64)

echo "=== Building lower_all variants ==="
for lp in "${PRECISIONS[@]}"; do
    echo "Building lower_all $lp..."
    _RELEASE=1 _LOWPREC=$lp _LOWER_ALL=1 _REDUCE_BITWIDTH_TRANSFORMATION=1 python -m utils.stages.compile_gpu_stage8 --optimize --compile
    mv "velocity_gpu_stage8_standalone_release.${lp}" "velocity_gpu_stage8_standalone_release.${lp}.all"
    echo "  -> velocity_gpu_stage8_standalone_release.${lp}.all"
done

echo "=== Building listed-lowering variants ==="
for lp in "${PRECISIONS[@]}"; do
    echo "Building listed $lp..."
    _RELEASE=1 _LOWPREC=$lp _REDUCE_BITWIDTH_TRANSFORMATION=1 python -m utils.stages.compile_gpu_stage8 --optimize --compile
    echo "  -> velocity_gpu_stage8_standalone_release.${lp}"
done

echo "All 6 binaries built."
