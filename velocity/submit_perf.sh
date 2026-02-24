#!/bin/bash
#SBATCH --job-name=velocity_perf
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=12
#SBATCH --partition=normal
#SBATCH --constraint=gpu
#SBATCH --hint=nomultithread
#SBATCH --time=12:00:00

set -e

DB_FILE=perf.db
TIMESTAMPS=(1 2 7 9 43 93 463 519 1140 1814 2593 5701 6519)
SUBSTEPS_LIST=(1 100)
PRECISIONS=(fp16 fp32 fp64)

echo "1) Building binaries..."
for lp in "${PRECISIONS[@]}"; do
    echo "Building $lp..."
    _RELEASE=1 _LOWPREC=$lp python -m utils.stages.compile_gpu_stage8 --optimize --compile
done

echo "2) Running all TS x LP x SUBSTEPS..."
for ts in "${TIMESTAMPS[@]}"; do
    for ss in "${SUBSTEPS_LIST[@]}"; do
        for lp in "${PRECISIONS[@]}"; do
            echo ">>> TS: $ts | LP: $lp | SUBSTEPS: $ss"
            SUBSTEPS=$ss ./velocity_gpu_stage8_standalone_release.${lp} "$ts" "${DB_FILE}"
        done
    done
done

echo "All tasks complete."
