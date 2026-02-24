# Stage 8 Velocity Kernel Benchmarking Guide

This guide covers the commands required to build, run, and validate the Stage 8 optimized velocity advection kernels.

## 1. Build Commands

### Build Debug Version
This build includes DaCe host-side timers (`_PROFILE=1`) and disables high-level compiler optimizations. Useful for step-by-step performance analysis.
```bash
_LOWPREC=fp32 python -m utils.stages.compile_gpu_stage8 --optimize --compile
```
**Binary produced:** `velocity_gpu.stage8_standalone_debug`

### Build Optimized Version
This build enables all performance transformations (Tiling, Bitwidth reduction), uses FP32 for storage, and enables `nvcc -O3`. 

You can optionally tune hardware-specific parameters like **coarsening** and **block sizes**:
```bash
# Example with 4x vertical coarsening and 128 thread block size
_RELEASE=1 _LOWPREC=fp32 _TILE=1 _REDUCE_BITWIDTH_TRANSFORMATION=1 \
X_COARSENING=1 Y_COARSENING=4 X_BLOCK_SIZE=128 \
python -m utils.stages.compile_gpu_stage8 --optimize --compile
```
**Binary produced:** `velocity_gpu.stage8_standalone_release`

---

## 2. Running the Binaries

Binaries can be run for a specific timestep or the default set. You can control repetition and substepping via environment variables.

### Standard Run (1 substep, 20 reps)
```bash
./velocity_gpu.stage8_standalone_release 2
```

### Discretization Error Analysis (Substepping)
To simulate the evolution of the advective field over 100 small substeps:
```bash
SUBSTEPS=100 ./velocity_gpu.stage8_standalone_release 2
```

---

## 3. Comparison and Validation

After running a binary, the results are serialized to the `gotwant/data_nproma<NPROMA>/` directory. Use the enhanced comparison script to analyze the numerical quality.

### Compare Specific Timestep
```bash
# Example for timestep 1
python utils/compare_got_and_want.py -r gotwant/data_nproma20480 2
```

### Compare All Timesteps
```bash
python utils/compare_got_and_want.py -r gotwant/data_nproma20480
```

The comparison will output a summary table including **RMSE**, **MAE**, **Mismatch Count**, and **SNR (dB)**.
