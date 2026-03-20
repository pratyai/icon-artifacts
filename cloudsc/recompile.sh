#!/bin/bash
set -e
rm -f cloudsc_gpu_bin
nvcc -O3 -std=c++20 \
    -Icodegen -Iinclude -I/Users/pmz/gitspace/ico2/dace/dace/runtime/include -I/opt/homebrew/Cellar/hdf5/2.1.0/include \
    cloudsc_main.cu codegen/*.cpp codegen/*.cu \
    -o cloudsc_gpu_bin -L/opt/homebrew/Cellar/hdf5/2.1.0/lib -lhdf5 -lcudart -lpthread
