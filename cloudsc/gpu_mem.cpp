#include "gpu_mem.h"
#include <atomic>
#include <map>
#include <mutex>
#include <cuda_runtime.h>

std::atomic<size_t> g_gpu_mem_usage{0};
static std::map<void*, size_t> g_allocs;
static std::mutex g_mem_mutex;

void track_gpu_malloc_internal(void* ptr, size_t bytes) {
    if (!ptr) return;
    std::lock_guard<std::mutex> lock(g_mem_mutex);
    g_allocs[ptr] = bytes;
    g_gpu_mem_usage += bytes;
}

void track_gpu_free_internal(void* ptr) {
    if (!ptr) return;
    std::lock_guard<std::mutex> lock(g_mem_mutex);
    auto it = g_allocs.find(ptr);
    if (it != g_allocs.end()) {
        g_gpu_mem_usage -= it->second;
        g_allocs.erase(it);
    }
}

size_t get_gpu_mem_usage() {
    return g_gpu_mem_usage.load();
}

// Linker Wrappers
extern "C" {
    cudaError_t __real_cudaMalloc(void** ptr, size_t size);
    cudaError_t __real_cudaFree(void* ptr);

    cudaError_t __wrap_cudaMalloc(void** ptr, size_t size) {
        cudaError_t err = __real_cudaMalloc(ptr, size);
        if (err == cudaSuccess) {
            track_gpu_malloc_internal(*ptr, size);
        }
        return err;
    }

    cudaError_t __wrap_cudaFree(void* ptr) {
        track_gpu_free_internal(ptr);
        return __real_cudaFree(ptr);
    }
}
