// fp16_operators.h — Mixed-type arithmetic operators for dace::float16 / double.
// Resolves ambiguous operator errors when fp16 and fp64 values appear in the
// same expression (e.g. inside DaCe-generated tasklets with mixed-precision inputs).
// All mixed ops promote to float (not double) so GPU ALU stays in fp32.
#pragma once

#include <dace/types.h>

#ifdef __CUDACC__

// double OP dace::float16  →  float
__device__ __host__ inline float operator+(double a, dace::float16 b) {
    return static_cast<float>(a) + static_cast<float>(b);
}
__device__ __host__ inline float operator-(double a, dace::float16 b) {
    return static_cast<float>(a) - static_cast<float>(b);
}
__device__ __host__ inline float operator*(double a, dace::float16 b) {
    return static_cast<float>(a) * static_cast<float>(b);
}
__device__ __host__ inline float operator/(double a, dace::float16 b) {
    return static_cast<float>(a) / static_cast<float>(b);
}

// dace::float16 OP double  →  float
__device__ __host__ inline float operator+(dace::float16 a, double b) {
    return static_cast<float>(a) + static_cast<float>(b);
}
__device__ __host__ inline float operator-(dace::float16 a, double b) {
    return static_cast<float>(a) - static_cast<float>(b);
}
__device__ __host__ inline float operator*(dace::float16 a, double b) {
    return static_cast<float>(a) * static_cast<float>(b);
}
__device__ __host__ inline float operator/(dace::float16 a, double b) {
    return static_cast<float>(a) / static_cast<float>(b);
}

// Comparison operators
__device__ __host__ inline bool operator<(double a, dace::float16 b) {
    return static_cast<float>(a) < static_cast<float>(b);
}
__device__ __host__ inline bool operator<(dace::float16 a, double b) {
    return static_cast<float>(a) < static_cast<float>(b);
}
__device__ __host__ inline bool operator>(double a, dace::float16 b) {
    return static_cast<float>(a) > static_cast<float>(b);
}
__device__ __host__ inline bool operator>(dace::float16 a, double b) {
    return static_cast<float>(a) > static_cast<float>(b);
}
__device__ __host__ inline bool operator<=(double a, dace::float16 b) {
    return static_cast<float>(a) <= static_cast<float>(b);
}
__device__ __host__ inline bool operator<=(dace::float16 a, double b) {
    return static_cast<float>(a) <= static_cast<float>(b);
}
__device__ __host__ inline bool operator>=(double a, dace::float16 b) {
    return static_cast<float>(a) >= static_cast<float>(b);
}
__device__ __host__ inline bool operator>=(dace::float16 a, double b) {
    return static_cast<float>(a) >= static_cast<float>(b);
}

// Math function overloads for dace::float16
// NOTE: exp(half) and max(half,half) are already provided by DaCe's halfvec.cuh
__device__ __host__ inline dace::float16 abs(dace::float16 a) {
    return static_cast<dace::float16>(fabsf(static_cast<float>(a)));
}
__device__ __host__ inline dace::float16 fabs(dace::float16 a) {
    return static_cast<dace::float16>(fabsf(static_cast<float>(a)));
}
__device__ __host__ inline dace::float16 sqrt(dace::float16 a) {
    return static_cast<dace::float16>(sqrtf(static_cast<float>(a)));
}
__device__ __host__ inline dace::float16 log(dace::float16 a) {
    return static_cast<dace::float16>(logf(static_cast<float>(a)));
}
__device__ __host__ inline dace::float16 min(dace::float16 a, dace::float16 b) {
    return static_cast<dace::float16>(fminf(static_cast<float>(a), static_cast<float>(b)));
}
__device__ __host__ inline float min(double a, dace::float16 b) {
    return fminf(static_cast<float>(a), static_cast<float>(b));
}
__device__ __host__ inline float min(dace::float16 a, double b) {
    return fminf(static_cast<float>(a), static_cast<float>(b));
}
__device__ __host__ inline float max(double a, dace::float16 b) {
    return fmaxf(static_cast<float>(a), static_cast<float>(b));
}
__device__ __host__ inline float max(dace::float16 a, double b) {
    return fmaxf(static_cast<float>(a), static_cast<float>(b));
}

#endif // __CUDACC__
