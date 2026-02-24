#ifndef BFP_CUH
#define BFP_CUH

#include <cmath>
#include <cstdint>
#include <cstring>

// Block Floating Point (BFP) compression for read-only GPU arrays.
//
// Format: packed buffer of blocks, each block = [exp (1 byte)][mantissas...]
//   MANTISSA_BITS=16: int16 mantissa (2 bytes each), bias=15, block=65 bytes
//   MANTISSA_BITS=8:  int8  mantissa (1 byte each),  bias=7,  block=33 bytes
// Decode: value = ldexpf((float)mantissa, exponent - bias)

template <int BLOCK_SIZE = 32, int MANTISSA_BITS = 16>
__device__ __forceinline__ float bfp_decode(const uint8_t *__restrict__ packed,
                                            int flat_idx) {
  constexpr int MBYTES = MANTISSA_BITS / 8;
  constexpr int BIAS = MANTISSA_BITS - 1;
  constexpr int BLOCK_BYTES = 1 + BLOCK_SIZE * MBYTES;
  int block = flat_idx / BLOCK_SIZE;
  int intra = flat_idx % BLOCK_SIZE;
  int bstart = block * BLOCK_BYTES;
  int8_t exp = static_cast<int8_t>(packed[bstart]);
  float mant;
  if constexpr (MANTISSA_BITS == 16) {
    int16_t m;
    memcpy(&m, packed + bstart + 1 + intra * 2, sizeof(int16_t));
    mant = static_cast<float>(m);
  } else {
    int8_t m = static_cast<int8_t>(packed[bstart + 1 + intra]);
    mant = static_cast<float>(m);
  }
  return ldexpf(mant, static_cast<int>(exp) - BIAS);
}

#ifndef __CUDACC__
// Host-only utilities

#include <algorithm>

template <int BLOCK_SIZE = 32, int MANTISSA_BITS = 16>
inline int64_t bfp_packed_size(int64_t n_elements) {
  constexpr int MBYTES = MANTISSA_BITS / 8;
  constexpr int BLOCK_BYTES = 1 + BLOCK_SIZE * MBYTES;
  int64_t num_blocks = (n_elements + BLOCK_SIZE - 1) / BLOCK_SIZE;
  return num_blocks * BLOCK_BYTES;
}

template <int BLOCK_SIZE = 32, int MANTISSA_BITS = 16>
inline void bfp_pack(const double *src, uint8_t *dst, int64_t N) {
  constexpr int MBYTES = MANTISSA_BITS / 8;
  constexpr int BIAS = MANTISSA_BITS - 1;
  constexpr int MANT_MIN = -(1 << (MANTISSA_BITS - 1));
  constexpr int MANT_MAX = (1 << (MANTISSA_BITS - 1)) - 1;
  constexpr int BLOCK_BYTES = 1 + BLOCK_SIZE * MBYTES;
  int64_t num_blocks = (N + BLOCK_SIZE - 1) / BLOCK_SIZE;

  for (int64_t b = 0; b < num_blocks; b++) {
    int64_t start = b * BLOCK_SIZE;
    int64_t end = std::min(start + static_cast<int64_t>(BLOCK_SIZE), N);
    int64_t bstart = b * BLOCK_BYTES;

    // Pass 1: find max absolute value in block
    float max_abs = 0.0f;
    for (int64_t i = start; i < end; i++) {
      float v = std::fabs(static_cast<float>(src[i]));
      if (v > max_abs)
        max_abs = v;
    }

    // Compute shared exponent
    int shared_exp;
    if (max_abs == 0.0f) {
      shared_exp = 0;
    } else {
      int fexp;
      std::frexp(max_abs, &fexp);
      shared_exp = fexp + BIAS;
    }
    shared_exp = std::max(-128, std::min(127, shared_exp));

    // Write exponent
    dst[bstart] = static_cast<uint8_t>(static_cast<int8_t>(shared_exp));

    // Pass 2: quantize mantissas
    float scale = std::ldexp(1.0f, -(shared_exp - BIAS));
    for (int64_t i = start; i < end; i++) {
      float scaled = static_cast<float>(src[i]) * scale;
      int32_t q = static_cast<int32_t>(std::round(scaled));
      q = std::max(static_cast<int32_t>(MANT_MIN),
                   std::min(static_cast<int32_t>(MANT_MAX), q));
      if constexpr (MANTISSA_BITS == 16) {
        int16_t mant = static_cast<int16_t>(q);
        memcpy(dst + bstart + 1 + (i - start) * 2, &mant, sizeof(int16_t));
      } else {
        dst[bstart + 1 + (i - start)] = static_cast<uint8_t>(static_cast<int8_t>(q));
      }
    }

    // Zero-pad if last block is partial
    for (int64_t i = end; i < start + BLOCK_SIZE; i++) {
      if constexpr (MANTISSA_BITS == 16) {
        int16_t zero = 0;
        memcpy(dst + bstart + 1 + (i - start) * 2, &zero, sizeof(int16_t));
      } else {
        dst[bstart + 1 + (i - start)] = 0;
      }
    }
  }
}

#else
// Device-side stubs so bfp_packed_size / bfp_pack can be called in host code
// compiled by nvcc (they are host-only but nvcc sees the whole file)

template <int BLOCK_SIZE = 32, int MANTISSA_BITS = 16>
inline int64_t bfp_packed_size(int64_t n_elements) {
  constexpr int MBYTES = MANTISSA_BITS / 8;
  constexpr int BLOCK_BYTES = 1 + BLOCK_SIZE * MBYTES;
  int64_t num_blocks = (n_elements + BLOCK_SIZE - 1) / BLOCK_SIZE;
  return num_blocks * BLOCK_BYTES;
}

template <int BLOCK_SIZE = 32, int MANTISSA_BITS = 16>
__host__ inline void bfp_pack(const double *src, uint8_t *dst, int64_t N) {
  constexpr int MBYTES = MANTISSA_BITS / 8;
  constexpr int BIAS = MANTISSA_BITS - 1;
  constexpr int MANT_MIN = -(1 << (MANTISSA_BITS - 1));
  constexpr int MANT_MAX = (1 << (MANTISSA_BITS - 1)) - 1;
  constexpr int BLOCK_BYTES = 1 + BLOCK_SIZE * MBYTES;
  int64_t num_blocks = (N + BLOCK_SIZE - 1) / BLOCK_SIZE;

  for (int64_t b = 0; b < num_blocks; b++) {
    int64_t start = b * BLOCK_SIZE;
    int64_t end = (start + BLOCK_SIZE < N) ? start + BLOCK_SIZE : N;
    int64_t bstart = b * BLOCK_BYTES;

    float max_abs = 0.0f;
    for (int64_t i = start; i < end; i++) {
      float v = fabsf(static_cast<float>(src[i]));
      if (v > max_abs)
        max_abs = v;
    }

    int shared_exp;
    if (max_abs == 0.0f) {
      shared_exp = 0;
    } else {
      int fexp;
      frexpf(max_abs, &fexp);
      shared_exp = fexp + BIAS;
    }
    if (shared_exp < -128)
      shared_exp = -128;
    if (shared_exp > 127)
      shared_exp = 127;

    dst[bstart] = static_cast<uint8_t>(static_cast<int8_t>(shared_exp));

    float scale = ldexpf(1.0f, -(shared_exp - BIAS));
    for (int64_t i = start; i < end; i++) {
      float scaled = static_cast<float>(src[i]) * scale;
      int32_t q = static_cast<int32_t>(roundf(scaled));
      if (q < MANT_MIN)
        q = MANT_MIN;
      if (q > MANT_MAX)
        q = MANT_MAX;
      if constexpr (MANTISSA_BITS == 16) {
        int16_t mant = static_cast<int16_t>(q);
        memcpy(dst + bstart + 1 + (i - start) * 2, &mant, sizeof(int16_t));
      } else {
        dst[bstart + 1 + (i - start)] = static_cast<uint8_t>(static_cast<int8_t>(q));
      }
    }

    for (int64_t i = end; i < start + BLOCK_SIZE; i++) {
      if constexpr (MANTISSA_BITS == 16) {
        int16_t zero = 0;
        memcpy(dst + bstart + 1 + (i - start) * 2, &zero, sizeof(int16_t));
      } else {
        dst[bstart + 1 + (i - start)] = 0;
      }
    }
  }
}

#endif // __CUDACC__

#endif // BFP_CUH
