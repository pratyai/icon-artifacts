#pragma once

#include <chrono>
#include <iostream>
#include <vector>
#include <string>

struct TimingResult {
  std::string tag;
  double time_us;
};

void measure_time(const char *tag = nullptr);
std::vector<TimingResult> drain_timing_results();
