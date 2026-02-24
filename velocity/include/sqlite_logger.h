#pragma once

#include <string>
#include <vector>

#define SQLITE_LOGGER_STRINGIFY(x) #x
#define SQLITE_LOGGER_TOSTRING(x) SQLITE_LOGGER_STRINGIFY(x)

template <typename T> inline std::string precision_name() {
  if constexpr (std::is_same_v<T, double>)
    return "fp64";
  if constexpr (std::is_same_v<T, float>)
    return "fp32";
  if constexpr (std::is_same_v<T, half>)
    return "fp16";
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
