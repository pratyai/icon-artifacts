#!/bin/bash
set -e
c++ -O3 -std=c++20 -DNDEBUG -Wno-parentheses-equality -Icodegen -Iinclude -I/Users/pmz/gitspace/ico2/cloudsc/.venv/lib/python3.14/site-packages/dace/runtime/include -I/opt/homebrew/Cellar/hdf5/2.1.0/include cloudsc_main.cpp codegen/*.cpp -o cloudsc_cpu_bin -lpthread -L/opt/homebrew/Cellar/hdf5/2.1.0/lib -lhdf5
