#pragma once

#include <string>
#include <vector>
#include <type_traits>

#include <dace/types.h>

#define SQLITE_LOGGER_STRINGIFY(x) #x
#define SQLITE_LOGGER_TOSTRING(x) SQLITE_LOGGER_STRINGIFY(x)

#include <typeinfo>
#include <iostream>

template <typename T> inline std::string precision_name() {
  using U = std::decay_t<T>;
  if constexpr (std::is_same_v<U, double>)
    return "fp64";
  if constexpr (std::is_same_v<U, float>)
    return "fp32";
  if constexpr (std::is_same_v<U, dace::float16>)
    return "fp16";

  std::cerr << "CRITICAL ERROR: Unknown precision type detected: " << typeid(T).name() << std::endl;
  std::abort();
  return "unknown";
}

struct RunConfig {
  std::string lowerprec;
  int substeps;
  int timestamp;
  int nproma;
  int istep;
  int lvn_only;
  int ldeepatmo;
  std::string db_file;
};

void save_field_to_db(const RunConfig &cfg, const std::string &field_name,
                      const void *got_data, size_t got_size,
                      const void *want_data, size_t want_size);

void save_timing_to_db(const RunConfig &cfg, int repetition,
                       const std::string &tag, double time_us);

void save_gpu_mem_to_db(const RunConfig &cfg, size_t bytes_transferred,
                        size_t bytes_allocated, size_t bytes_real,
                        size_t bytes_residency_increase);
