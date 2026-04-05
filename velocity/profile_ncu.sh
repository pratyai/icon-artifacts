#!/bin/bash
#SBATCH --job-name=ncu-profile
#SBATCH --account=g34
#SBATCH --time=02:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --partition=normal
#SBATCH --gres=gpu:1

# Usage: sbatch profile_ncu.sh <precision> <timestep> <data_dir> <output_dir>
# Example: sbatch profile_ncu.sh f32 2 data_r02b03 tmp/vt_profiles-r02b03

if [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ] || [ -z "$4" ]; then
    echo "Usage: sbatch $0 <precision: f16|f32|f64> <timestep> <data_dir> <output_dir>"
    exit 1
fi

PREC="$1"
TSTEP="$2"
DATA="$3"
OUTDIR="$4"

mkdir -p "$OUTDIR"

ulimit -s unlimited

# Use --set full alone (no --metrics).  NCU 2025.2.0.0 treats explicit
# --metrics as a *replacement* for the set's counters, dropping raw .sum
# values.  Raw FLOP instruction counts (smsp__sass_thread_inst_executed_op_
# *_pred_on.sum) can be reconstructed from .sum.per_cycle_elapsed × cycles
# which --set full does collect.  See extract_ncu.py for reconstruction.
ncu --set full --import-source yes \
    -o "${OUTDIR}/vt.${PREC}" -f \
    "./velocity_gpu_stage8_standalone_release.${PREC}" "$TSTEP" --reps=3 --data "$DATA" --skip-gotwant
