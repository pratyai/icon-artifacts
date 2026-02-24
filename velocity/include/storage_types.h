#ifndef __STORAGE_TYPES_H__
#define __STORAGE_TYPES_H__

// Define storage types for read-only arrays to allow controllable compression.
// Defaults to double (FP64) if not specified via -DLOW_PREC_TYPE during
// compilation.

#ifndef LOW_PREC_TYPE
#define LOW_PREC_TYPE double
#endif

// Support for 16-bit floats in CUDA
#if defined(__CUDACC__)
#include <cuda_fp16.h>
#endif

// Metrics (All in GRID_METRICS or INTERPOLATION_COEFFS lists in Stage 8 script)
using inv_dual_edge_length_storage_t = LOW_PREC_TYPE;
using ddqz_z_half_storage_t = LOW_PREC_TYPE;
using ddqz_z_full_e_storage_t = LOW_PREC_TYPE;
using wgtfac_e_storage_t = LOW_PREC_TYPE;
using wgtfac_c_storage_t = LOW_PREC_TYPE;
using coeff1_dwdz_storage_t = LOW_PREC_TYPE;
using coeff2_dwdz_storage_t = LOW_PREC_TYPE;
using rbf_vec_coeff_e_storage_t = LOW_PREC_TYPE;
using e_bln_c_s_storage_t = LOW_PREC_TYPE;
using c_lin_e_storage_t = LOW_PREC_TYPE;
using ddxn_z_full_storage_t = LOW_PREC_TYPE;
using ddxt_z_full_storage_t = LOW_PREC_TYPE;

#endif // __STORAGE_TYPES_H__
