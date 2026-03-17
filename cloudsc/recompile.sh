#!/bin/bash
set -e

# Remove old binary if it exists
rm -f cloudsc_cpu_bin

# Compile
c++ -O3 -g -std=c++20 -DNDEBUG -Wall -Wextra -Wno-parentheses-equality -Wno-unused-parameter \
    -Icodegen -Iinclude -I/Users/pmz/gitspace/ico2/dace/dace/runtime/include -I/opt/homebrew/Cellar/hdf5/2.1.0/include \
    cloudsc_main.cpp codegen/*.cpp \
    -o cloudsc_cpu_bin -lpthread -L/opt/homebrew/Cellar/hdf5/2.1.0/lib -lhdf5
