#ifndef __STORAGE_TYPES_H__
#define __STORAGE_TYPES_H__

// Define storage types for read-only arrays to allow controllable compression.
// Defaults to double (FP64) if not specified via -DLOW_PREC_TYPE during compilation.

#ifndef LOW_PREC_TYPE
#define LOW_PREC_TYPE double
#endif

typedef LOW_PREC_TYPE inv_dual_edge_length_storage_t;

#endif // __STORAGE_TYPES_H__
