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

# --set full alone does not collect the half-precision instruction counters
# (smsp__sass_thread_inst_executed_op_h{fma,mul,add}_pred_on.sum), so the FLOP
# share columns cannot be derived from it — only the fp32/fp64 counters appear,
# and then only in .sum.per_cycle_elapsed form.  Request the counters that
# extract_ncu.py reads explicitly; they coexist with the set's raw .sum values,
# so the memory columns are unaffected.
NCU_METRICS="dram__bytes.sum,dram__bytes_read.sum,dram__bytes_write.sum,l1tex__average_t_sectors_per_request_pipe_lsu_mem_global_op_ld.ratio,l1tex__average_t_sectors_per_request_pipe_lsu_mem_global_op_st.ratio,l1tex__t_bytes_lookup_hit.sum,l1tex__t_bytes_lookup_miss.sum,launch__shared_mem_per_block_dynamic,launch__shared_mem_per_block_static,lts__t_bytes_equiv_l1sectormiss_pipe_lsu_mem_global_op_ld.sum,lts__t_bytes_equiv_l1sectormiss_pipe_lsu_mem_global_op_st.sum,lts__t_bytes_lookup_hit.sum,lts__t_bytes_lookup_miss.sum,lts__t_sectors_lookup_hit.sum,lts__t_sectors_lookup_miss.sum,sm__sass_thread_inst_executed_op_memory_pred_on.sum,sm__warps_active.avg.per_cycle_active,sm__warps_active.sum,smsp__inst_executed.sum,smsp__inst_executed_op_conversion.sum,smsp__inst_executed_op_fp16.sum,smsp__inst_executed_op_fp32.sum,smsp__inst_executed_op_fp64.sum,smsp__inst_executed_op_global_ld.sum,smsp__inst_executed_op_global_st.sum,smsp__inst_executed_op_integer.sum,smsp__inst_executed_op_shared_ld.sum,smsp__inst_executed_op_shared_st.sum,smsp__sass_average_data_bytes_per_sector_mem_global_op_ld.pct,smsp__sass_average_data_bytes_per_sector_mem_global_op_st.pct,smsp__sass_thread_inst_executed_op_dadd_pred_on.sum,smsp__sass_thread_inst_executed_op_dfma_pred_on.sum,smsp__sass_thread_inst_executed_op_dmul_pred_on.sum,smsp__sass_thread_inst_executed_op_fadd_pred_on.sum,smsp__sass_thread_inst_executed_op_ffma_pred_on.sum,smsp__sass_thread_inst_executed_op_fmul_pred_on.sum,smsp__sass_thread_inst_executed_op_hadd_pred_on.sum,smsp__sass_thread_inst_executed_op_hfma_pred_on.sum,smsp__sass_thread_inst_executed_op_hmul_pred_on.sum,smsp__warps_launched.sum"

ncu --set full --import-source yes --metrics "$NCU_METRICS" \
    -o "${OUTDIR}/vt.${PREC}" -f \
    "./velocity_gpu_stage8_standalone_release.${PREC}" "$TSTEP" --reps=3 --data "$DATA" --skip-gotwant
