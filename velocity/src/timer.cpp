#include "timer.h"
#include <iostream>
#include <chrono>
#include <vector>
#include <mutex>

static std::vector<TimingResult> g_timing_results;
static std::mutex g_timing_mutex;

void measure_time(const char *tag)
{
  static int timer_call_count = 0;
  static std::chrono::high_resolution_clock::time_point timer_last_time = std::chrono::high_resolution_clock::now();

  if (timer_call_count % 2 == 1)
  {
    auto now = std::chrono::high_resolution_clock::now();
    long long start = std::chrono::duration_cast<std::chrono::microseconds>(timer_last_time.time_since_epoch()).count();
    long long end = std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()).count();
    double time_us = static_cast<double>(end - start);
    
    {
      std::lock_guard<std::mutex> lock(g_timing_mutex);
      g_timing_results.push_back({tag ? tag : "unknown", time_us});
    }
    
    std::cout << "Timer " << (tag ? tag : "unknown") << " took " << time_us << " us" << std::endl;
  } else {
    timer_last_time = std::chrono::high_resolution_clock::now();
  }

  timer_call_count++;
}

std::vector<TimingResult> drain_timing_results() {
  std::lock_guard<std::mutex> lock(g_timing_mutex);
  std::vector<TimingResult> results = std::move(g_timing_results);
  g_timing_results.clear();
  return results;
}
