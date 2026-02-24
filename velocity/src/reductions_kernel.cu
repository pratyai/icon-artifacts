#include <cuda_runtime.h>
#include <cuda_fp16.h>
#include <cub/cub.cuh>

////////////////////////////////////////////////////
// We are running on host, but the data is on device
////////////////////////////////////////////////////


// --- Device-side helpers (used by batched kernels, kept as double) ---

__device__ __forceinline__ int shared_data_reduce_sum_v2(int* __restrict__ shared_data)
{
    constexpr int NUM_THREADS = 1024;
    constexpr int NUM_WARPS{NUM_THREADS / 32};
    int sum{0};
#pragma unroll
    for (int i{0}; i < NUM_WARPS; ++i)
    {
        sum += shared_data[i];
    }
    return sum;
}

__device__ __forceinline__ double shared_data_reduce_max_v2(double* __restrict__ shared_data)
{
    constexpr int NUM_THREADS = 1024;
    constexpr int NUM_WARPS{NUM_THREADS / 32};
    double max{-1.0e300};
#pragma unroll
    for (int i{0}; i < NUM_WARPS; ++i)
    {
        max = shared_data[i] > max ? shared_data[i] : max;
    }
    return max;
}


__device__ __forceinline__ int warp_reduce_sum(int val)
{
    constexpr unsigned int FULL_MASK{0xffffffff};
#pragma unroll
    for (int offset{16}; offset > 0; offset /= 2)
    {
        val += __shfl_down_sync(FULL_MASK, val, offset);
    }
    return val;
}

__device__ __forceinline__ double warp_reduce_max(double val)
{
    constexpr unsigned int FULL_MASK{0xffffffff};
    #pragma unroll
    for (int offset{16}; offset > 0; offset /= 2)
    {
        val = max(val, __shfl_down_sync(FULL_MASK, val, offset));
    }
    return val;
}

__device__ __forceinline__ int block_reduce_sum_v2(int const* __restrict__ input_data,
                                    int* __restrict__ shared_data,
                                    int num_elements)
{
    constexpr int NUM_THREADS = 1024;
    const int num_elements_per_thread{(num_elements + NUM_THREADS - 1) / NUM_THREADS};
    const int thread_idx{threadIdx.x};
    int sum{0};
    for (int i{0}; i < num_elements_per_thread; ++i)
    {
        int const offset{thread_idx + i * NUM_THREADS};
        if (offset < num_elements)
        {
            sum += input_data[offset];
        }
    }
    sum = warp_reduce_sum(sum);
    if (threadIdx.x % 32 == 0)
    {
        shared_data[threadIdx.x / 32] = sum;
    }
    __syncthreads();
    int const block_sum{shared_data_reduce_sum_v2(shared_data)};
    return block_sum;
}

__device__ __forceinline__ double block_reduce_max_v2(double const* __restrict__ input_data,
                                    double* __restrict__ shared_data,
                                    int num_elements)
{
    constexpr int NUM_THREADS = 1024;
    const int num_elements_per_thread{(num_elements + NUM_THREADS - 1) / NUM_THREADS};
    const int thread_idx{threadIdx.x};
    double max{-1.0e300};
    for (int i{0}; i < num_elements_per_thread; ++i)
    {
        int const offset{thread_idx + i * NUM_THREADS};
        if (offset < num_elements)
        {
            max = input_data[offset]  > max ? input_data[offset] : max;
        }
    }
    max = warp_reduce_max(max);
    if (threadIdx.x % 32 == 0)
    {
        shared_data[threadIdx.x / 32] = max;
    }
    __syncthreads();
    double const block_max{shared_data_reduce_max_v2(shared_data)};
    return block_max;
}


// --- Batched kernels (kept as concrete types for cudaLaunchKernel) ---

__global__ void batched_reduce_sum_v2(int* __restrict__ output_data,
                                      int const* __restrict__ input_data,
                                      int num_elements_per_batch)
{
    constexpr int NUM_THREADS = 1024;
    constexpr int NUM_WARPS{NUM_THREADS / 32};
    int const block_idx{blockIdx.x};
    int const thread_idx{threadIdx.x};
    __shared__ int shared_data[NUM_WARPS];
    int const block_sum{block_reduce_sum_v2(
        input_data + block_idx * num_elements_per_batch, shared_data,
        num_elements_per_batch)};
    if (thread_idx == 0)
    {
        output_data[block_idx] = block_sum;
    }
}

__global__ void batched_reduce_max_v2(double* __restrict__ output_data,
                                      double const* __restrict__ input_data,
                                      int num_elements_per_batch)
{
    constexpr int NUM_THREADS = 1024;
    constexpr int NUM_WARPS{NUM_THREADS / 32};
    int const block_idx{blockIdx.x};
    int const thread_idx{threadIdx.x};
    __shared__ double shared_data[NUM_WARPS];
    double const block_max{block_reduce_max_v2(
        input_data + block_idx * num_elements_per_batch, shared_data,
        num_elements_per_batch)};
    if (thread_idx == 0)
    {
        output_data[block_idx] = block_max;
    }
}


// --- Templated CUB-based reductions ---

// TIn: input array element type (e.g. float, __half)
// TOut: output element type (e.g. double for CFL scalars going back to Fortran)
// CUB reduces in TIn precision; result is cast to TOut.

template<typename TIn, typename TOut>
void reduce_maxZ_to_address_gpu(const TIn *__restrict__ d_in,
                                 TOut* __restrict__ d_out,
                                 int size,
                                 cudaStream_t stream)
{
  // CUB requires matching in/out types, so reduce into a TIn temporary
  static TIn* d_tmp = nullptr;
  static void* temp_storage = nullptr;
  static size_t temp_storage_bytes = 0;
  static int last_size = -1;

  if (d_tmp == nullptr) {
    cudaMalloc(&d_tmp, sizeof(TIn));
  }
  if (size > last_size) {
    if (temp_storage != nullptr) {
      cudaFree(temp_storage);
      temp_storage = nullptr;
    }
    temp_storage_bytes = 0;
    cub::DeviceReduce::Max(nullptr, temp_storage_bytes, d_in, d_tmp, size, nullptr);
    if (temp_storage_bytes != 0) {
      cudaMalloc(&temp_storage, temp_storage_bytes);
    }
    last_size = size;
  }

  cub::DeviceReduce::Max(temp_storage, temp_storage_bytes, d_in, d_tmp, size, stream);

  // Copy with implicit cast TIn -> TOut on host
  if constexpr (std::is_same_v<TIn, TOut>) {
    cudaMemcpyAsync(d_out, d_tmp, sizeof(TOut), cudaMemcpyDeviceToDevice, stream);
  } else {
    TIn tmp_host;
    cudaMemcpyAsync(&tmp_host, d_tmp, sizeof(TIn), cudaMemcpyDeviceToHost, stream);
    cudaStreamSynchronize(stream);
    TOut tmp_cast = static_cast<TOut>(tmp_host);
    cudaMemcpyAsync(d_out, &tmp_cast, sizeof(TOut), cudaMemcpyHostToDevice, stream);
  }
}

template<typename TIn, typename TOut>
TOut reduce_maxZ_to_scalar_gpu(const TIn *__restrict__ d_in, int size, cudaStream_t stream)
{
  // CUB reduces in TIn precision, then cast to TOut
  static TIn* d_tmp = nullptr;
  if (d_tmp == nullptr) {
    cudaMalloc(&d_tmp, sizeof(TIn));
  }

  // Use the same-type overload for the CUB reduction
  reduce_maxZ_to_address_gpu<TIn, TIn>(d_in, d_tmp, size, stream);

  TIn tmp_host;
  cudaMemcpyAsync(&tmp_host, d_tmp, sizeof(TIn), cudaMemcpyDeviceToHost, stream);
  cudaStreamSynchronize(stream);
  return static_cast<TOut>(tmp_host);
}

template<typename TIn, typename TOut>
void reduce_sum_to_address_gpu(const TIn *__restrict__ d_in,
                                 TOut* __restrict__ d_out,
                                 int size,
                                 cudaStream_t stream)
{
  static TIn* d_tmp = nullptr;
  static void* temp_storage = nullptr;
  static size_t temp_storage_bytes = 0;
  static int last_size = -1;

  if (d_tmp == nullptr) {
    cudaMalloc(&d_tmp, sizeof(TIn));
  }
  if (temp_storage == nullptr || size != last_size) {
    if (temp_storage != nullptr) {
      cudaFree(temp_storage);
      temp_storage = nullptr;
    }
    temp_storage_bytes = 0;
    cub::DeviceReduce::Sum(nullptr, temp_storage_bytes, d_in, d_tmp, size, nullptr);
    if (temp_storage_bytes != 0) {
      cudaMalloc(&temp_storage, temp_storage_bytes);
    }
    last_size = size;
  }

  cub::DeviceReduce::Sum(temp_storage, temp_storage_bytes, d_in, d_tmp, size, stream);

  if constexpr (std::is_same_v<TIn, TOut>) {
    cudaMemcpyAsync(d_out, d_tmp, sizeof(TOut), cudaMemcpyDeviceToDevice, stream);
  } else {
    TIn tmp_host;
    cudaMemcpyAsync(&tmp_host, d_tmp, sizeof(TIn), cudaMemcpyDeviceToHost, stream);
    cudaStreamSynchronize(stream);
    TOut tmp_cast = static_cast<TOut>(tmp_host);
    cudaMemcpyAsync(d_out, &tmp_cast, sizeof(TOut), cudaMemcpyHostToDevice, stream);
  }
}

// Explicit instantiations — same-type (TIn == TOut)
template void reduce_maxZ_to_address_gpu<double, double>(const double*, double*, int, cudaStream_t);
template void reduce_maxZ_to_address_gpu<float, float>(const float*, float*, int, cudaStream_t);
template void reduce_maxZ_to_address_gpu<__half, __half>(const __half*, __half*, int, cudaStream_t);

// Cross-type: reduce in lowered precision, output to double
template void reduce_maxZ_to_address_gpu<float, double>(const float*, double*, int, cudaStream_t);
template void reduce_maxZ_to_address_gpu<__half, double>(const __half*, double*, int, cudaStream_t);

template double reduce_maxZ_to_scalar_gpu<double, double>(const double*, int, cudaStream_t);
template float reduce_maxZ_to_scalar_gpu<float, float>(const float*, int, cudaStream_t);
template __half reduce_maxZ_to_scalar_gpu<__half, __half>(const __half*, int, cudaStream_t);
// Cross-type: reduce in lowered precision, return double
template double reduce_maxZ_to_scalar_gpu<float, double>(const float*, int, cudaStream_t);
template double reduce_maxZ_to_scalar_gpu<__half, double>(const __half*, int, cudaStream_t);

template void reduce_sum_to_address_gpu<double, double>(const double*, double*, int, cudaStream_t);
template void reduce_sum_to_address_gpu<float, float>(const float*, float*, int, cudaStream_t);
template void reduce_sum_to_address_gpu<__half, __half>(const __half*, __half*, int, cudaStream_t);
template void reduce_sum_to_address_gpu<float, double>(const float*, double*, int, cudaStream_t);
template void reduce_sum_to_address_gpu<__half, double>(const __half*, double*, int, cudaStream_t);


// --- Non-templated int reductions ---

int reduce_sum_to_scalar_gpu(const int *__restrict__ d_in, int size, cudaStream_t stream)
{
  static void* temp_storage = nullptr;
  static int* d_out = nullptr;
  static size_t temp_storage_bytes = 0;
  static int last_size = -1;

  if (d_out == nullptr) {
    cudaMalloc(&d_out, sizeof(int));
  }
  if (size > last_size) {
    if (temp_storage != nullptr) {
      cudaFree(temp_storage);
      temp_storage = nullptr;
    }
    temp_storage_bytes = 0;
    cub::DeviceReduce::Sum(nullptr, temp_storage_bytes, d_in, d_out, size, nullptr);
    if (temp_storage_bytes != 0) {
      cudaMalloc(&temp_storage, temp_storage_bytes);
    }
    last_size = size;
  }
  cub::DeviceReduce::Sum(temp_storage, temp_storage_bytes, d_in, d_out, size, stream);
  int sumval;
  cudaMemcpyAsync(&sumval, d_out, sizeof(int), cudaMemcpyDeviceToHost, stream);
  cudaStreamSynchronize(stream);
  return sumval;
}

// scan reduction interface
int reduce_scan_gpu(const int *__restrict__ d_in, int size, cudaStream_t stream)
{
  return (reduce_sum_to_scalar_gpu(d_in, size, stream) > 0)? 1 : 0;
}

// scan reduction interface
int reduce_scan_gpu(int d_in, int size, cudaStream_t stream)
{
  return (d_in > 0)? 1 : 0;
}


void reduce_segmented_to_address_gpu(const int *__restrict__ d_in, int*__restrict__ d_out, int size, int batch_size, cudaStream_t stream)
{
    void *batched_reduce_sum_v2_args[] = {
        (void *)&d_out,
        (void *)&d_in,
        (void *)&size
    };

    cudaError_t err = cudaLaunchKernel(
        (void*)batched_reduce_sum_v2,
        dim3(batch_size, 1, 1),
        dim3(1024, 1, 1),
        (void**)batched_reduce_sum_v2_args,
        0,
        nullptr
    );
}
