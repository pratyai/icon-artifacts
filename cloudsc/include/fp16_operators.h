// fp16_operators.h — CUDA-specific helpers for dace::float16 mixed-precision.
//
// CPU-side common_type specializations and pow overloads now live in
// dace/types.h and dace/math.h respectively.
#pragma once

#include <dace/types.h>

#ifdef __CUDACC__

__device__ __host__ inline dace::float16 abs(dace::float16 a) {
    return __habs(a);
}
__device__ __host__ inline bool operator<(dace::float16 a, double b) {
   return a < static_cast<dace::float16>(b);
}
__device__ __host__ inline bool operator/(float a, dace::float16 b) {
    return static_cast<dace::float16>(a) / b;
}

#endif // __CUDACC__
