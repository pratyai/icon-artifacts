#!/bin/bash
#SBATCH --job-name=velocity_perf
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=12
#SBATCH --partition=normal
#SBATCH --constraint=gpu
#SBATCH --hint=nomultithread
#SBATCH --time=4:00:00

set -e

TIMESTAMPS=(1 2 7 9 43 93 463 519 1140 1814 2593 5701 6519)
SUBSTEPS_LIST=(1 2)
PRECISIONS=(fp16 fp32 fp64)

echo "1) Running lower_all variants -> perf-all.db"
for ts in "${TIMESTAMPS[@]}"; do
    for ss in "${SUBSTEPS_LIST[@]}"; do
        for lp in "${PRECISIONS[@]}"; do
            echo ">>> TS: $ts | SS: $ss | LP: $lp (all)"
            SUBSTEPS=$ss ./velocity_gpu_stage8_standalone_release.${lp}.all "$ts" perf-all.db
        done
    done
done

echo "2) Running listed-lowering variants -> perf.db"
for ts in "${TIMESTAMPS[@]}"; do
    for ss in "${SUBSTEPS_LIST[@]}"; do
        for lp in "${PRECISIONS[@]}"; do
            echo ">>> TS: $ts | SS: $ss | LP: $lp (listed)"
            SUBSTEPS=$ss ./velocity_gpu_stage8_standalone_release.${lp} "$ts" perf.db
        done
    done
done

echo "All tasks complete."
