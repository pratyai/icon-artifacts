// fp16_operators.h — Mixed-type arithmetic/comparison operators for
// dace::float16 vs double/float.  Resolves ambiguous operator errors when
// fp16 and fp64 values appear in the same expression (e.g. DaCe-generated
// tasklets with mixed-precision inputs).
// All mixed ops cast to float (not double) so GPU ALU stays in fp32.
#pragma once

#include <dace/types.h>

#ifdef __CUDACC__

// --- abs ---
__device__ __host__ inline dace::float16 abs(dace::float16 a) {
    return __habs(a);
}

// --- Comparison: half <op> double ---
__device__ __host__ inline bool operator<(dace::float16 a, double b) {
    return float(a) < float(b);
}
__device__ __host__ inline bool operator>(dace::float16 a, double b) {
    return float(a) > float(b);
}
__device__ __host__ inline bool operator<=(dace::float16 a, double b) {
    return float(a) <= float(b);
}
__device__ __host__ inline bool operator>=(dace::float16 a, double b) {
    return float(a) >= float(b);
}
__device__ __host__ inline bool operator==(dace::float16 a, double b) {
    return float(a) == float(b);
}
__device__ __host__ inline bool operator!=(dace::float16 a, double b) {
    return float(a) != float(b);
}

// --- Comparison: double <op> half ---
__device__ __host__ inline bool operator<(double a, dace::float16 b) {
    return float(a) < float(b);
}
__device__ __host__ inline bool operator>(double a, dace::float16 b) {
    return float(a) > float(b);
}
__device__ __host__ inline bool operator<=(double a, dace::float16 b) {
    return float(a) <= float(b);
}
__device__ __host__ inline bool operator>=(double a, dace::float16 b) {
    return float(a) >= float(b);
}
__device__ __host__ inline bool operator==(double a, dace::float16 b) {
    return float(a) == float(b);
}
__device__ __host__ inline bool operator!=(double a, dace::float16 b) {
    return float(a) != float(b);
}

// --- Arithmetic: half <op> double ---
__device__ __host__ inline float operator+(dace::float16 a, double b) {
    return float(a) + float(b);
}
__device__ __host__ inline float operator-(dace::float16 a, double b) {
    return float(a) - float(b);
}
__device__ __host__ inline float operator*(dace::float16 a, double b) {
    return float(a) * float(b);
}
__device__ __host__ inline float operator/(dace::float16 a, double b) {
    return float(a) / float(b);
}

// --- Arithmetic: double <op> half ---
__device__ __host__ inline float operator+(double a, dace::float16 b) {
    return float(a) + float(b);
}
__device__ __host__ inline float operator-(double a, dace::float16 b) {
    return float(a) - float(b);
}
__device__ __host__ inline float operator*(double a, dace::float16 b) {
    return float(a) * float(b);
}
__device__ __host__ inline float operator/(double a, dace::float16 b) {
    return float(a) / float(b);
}

// --- dace::math::pow overloads for float16 (two-type template can't be specialized) ---
namespace dace { namespace math {
    DACE_HDFI float pow(const double& a, const dace::float16& b) {
        return powf(float(a), float(b));
    }
    DACE_HDFI float pow(const dace::float16& a, const double& b) {
        return powf(float(a), float(b));
    }
    DACE_HDFI float pow(const float& a, const dace::float16& b) {
        return powf(a, float(b));
    }
    DACE_HDFI float pow(const dace::float16& a, const float& b) {
        return powf(float(a), b);
    }
    DACE_HDFI float pow(const dace::float16& a, const dace::float16& b) {
        return powf(float(a), float(b));
    }
    DACE_HDFI float pow(const dace::float16& a, const int& b) {
        return powf(float(a), float(b));
    }
}}

// --- Mixed max/min: half vs double ---
__device__ __host__ inline float max(dace::float16 a, double b) {
    return fmaxf(float(a), float(b));
}
__device__ __host__ inline float max(double a, dace::float16 b) {
    return fmaxf(float(a), float(b));
}
__device__ __host__ inline float min(dace::float16 a, double b) {
    return fminf(float(a), float(b));
}
__device__ __host__ inline float min(double a, dace::float16 b) {
    return fminf(float(a), float(b));
}
__device__ __host__ inline float pow(dace::float16 a, double b) {
    return powf(float(a), float(b));
}

// --- Arithmetic: float / half (not in DaCe's halfvec.cuh) ---
__device__ __host__ inline float operator/(float a, dace::float16 b) {
    return a / float(b);
}

#endif // __CUDACC__
