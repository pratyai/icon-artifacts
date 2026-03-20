#pragma once
#include <cuda_runtime.h>
#include <stddef.h>

// Low-level tracking functions
void track_gpu_malloc_internal(void* ptr, size_t bytes);
void track_gpu_free_internal(void* ptr);
size_t get_gpu_mem_usage();

// Note: Tracking is now handled at the linker level via --wrap=cudaMalloc
