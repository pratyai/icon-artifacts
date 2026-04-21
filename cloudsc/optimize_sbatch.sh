#!/bin/bash
#SBATCH --job-name=cloudsc-optimize
#SBATCH --account=g34
#SBATCH --time=04:00:00
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --partition=normal
#SBATCH --output=slurm-optimize-%j.out

# Usage: sbatch optimize_sbatch.sh [input.sdfgz] [-- optimize.py args...]
# Default input: cloudsc_pydace_simplified_symbolic.sdfgz
# Example:  sbatch optimize_sbatch.sh
#           sbatch optimize_sbatch.sh cloudsc_pydace_simplified_symbolic.sdfgz --start-from after_ssa

set -e

INPUT="${1:-cloudsc_pydace_simplified_symbolic.sdfgz}"
shift || true

# Source the local venv (activates dace-cloudsc editable install).
source .venv/bin/activate

ulimit -s unlimited

echo "[$(date +%H:%M:%S)] starting optimize.py on $INPUT"
python optimize.py "$INPUT" "$@"
echo "[$(date +%H:%M:%S)] done"
