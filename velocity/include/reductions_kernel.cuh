#pragma once

#include <cuda_runtime.h>
#include <cuda_fp16.h>

template<typename T>
void reduce_maxZ_to_address_gpu(const T *__restrict__ d_in, T* __restrict__ d_out, int size, cudaStream_t stream);

template<typename T>
T reduce_maxZ_to_scalar_gpu(const T *__restrict__ d_in, int size, cudaStream_t stream);

template<typename T>
void reduce_sum_to_address_gpu(const T *__restrict__ d_in, T* __restrict__ d_out, int size, cudaStream_t stream);

int reduce_sum_to_scalar_gpu(const int *__restrict__ d_in, int size, cudaStream_t stream);

int reduce_scan_gpu(int d_in, int size, cudaStream_t stream);
int reduce_scan_gpu(const int *__restrict__ d_in, int size, cudaStream_t stream);

__global__ void batched_reduce_sum_v2(int* __restrict__ output_data,
    int const* __restrict__ input_data,
    int num_elements_per_batch);
__global__ void batched_reduce_max_v2(double* __restrict__ output_data,
    double const* __restrict__ input_data,
    int num_elements_per_batch);

void reduce_segmented_to_address_gpu(const int *__restrict__ d_in, int*__restrict__ d_out, int size, int batch_size, cudaStream_t stream);

inline void cleanup_reduce_sum_gpu() {}
inline void cleanup_reduce_maxZ_gpu() {}
