# CUDA (nvcc) + sqlite/zstd/zlib build paths for the vt-gpu spack env.
# Source before building the VT .so on ault:
#     spack env activate vt-gpu
#     source arch/cscs/ault/build-env.sh

_loc() { spack -e vt-gpu find --format '{prefix}' "$1" | head -1; }
_cuda="$(_loc cuda)"
_sqlite="$(_loc sqlite)"
_zstd="$(_loc zstd)"
_zlib="$(_loc zlib)"

export GENCODE_ARCH="arch=compute_80,code=sm_80"   # A100
export PATH="${_cuda}/bin:${PATH}"
export PKG_CONFIG_PATH="${_sqlite}/lib/pkgconfig:${_zstd}/lib/pkgconfig:${_zlib}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export LIBRARY_PATH="${_sqlite}/lib:${_zstd}/lib:${_zlib}/lib:${_cuda}/lib64:${LIBRARY_PATH:-}"
export LD_LIBRARY_PATH="${LIBRARY_PATH}"
