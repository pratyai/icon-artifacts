// sensitivity.cuh — forward-difference sensitivity sweep.
//
// For each input field, multiplies it by (1+eps), re-runs the kernel, measures
// the RMS change in the output fields vs the unperturbed baseline, then
// restores the input. Reports a dimensionless amplification:
//   delta_out_rms / (eps * in_rms)
//
// ASSUMPTIONS:
//   - All field pointers are host pointers to double (fp64 compilation;
//     user error otherwise).  Perturbation and baseline capture happen on
//     the host; execute() handles H2D/kernel/D2H internally.
//   - execute() is called with a wrapped lambda that restores vn/w before
//     each invocation (see main_gpu.cu), so each perturbed run starts from
//     the same prognostic state as the baseline.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstring>
#include <cuda_runtime.h>
#include <functional>
#include <iomanip>
#include <iostream>
#include <random>
#include <string>
#include <vector>

namespace sensitivity {

struct Field {
  double *ptr;
  int n;
  std::string name;

  // Accept any pointer type; caller is responsible for fp64 correctness.
  template <typename T>
  Field(T *p, int n_, std::string s)
      : ptr(reinterpret_cast<double *>(p)), n(n_), name(std::move(s)) {}
};

inline void run(const std::function<void(double)> &execute, double dt,
                const std::vector<Field> &inputs,
                const std::vector<Field> &outputs, double eps = 1e-5,
                int n_samples = 10) {
  // Baseline run
  execute(dt);
  cudaDeviceSynchronize();

  long long out_n = 0;
  std::vector<std::vector<double>> baseline(outputs.size());
  for (size_t i = 0; i < outputs.size(); ++i) {
    baseline[i].resize(outputs[i].n);
    std::memcpy(baseline[i].data(), outputs[i].ptr,
                outputs[i].n * sizeof(double));
    out_n += outputs[i].n;
  }

  std::mt19937 rng(42);
  std::uniform_real_distribution<double> dist(-1.0, 1.0);

  // Per-input sweep with Monte Carlo random perturbations
  // Store (amplification, var_out, name) for each field
  struct Result {
    double amplification;
    double var_out;
    std::string name;
  };
  std::vector<Result> results;
  results.reserve(inputs.size());

  for (const auto &f : inputs) {
    if (!f.ptr || f.n <= 0)
      continue;
    std::vector<double> saved(f.n);
    std::memcpy(saved.data(), f.ptr, f.n * sizeof(double));

    double total_in_var = 0.0;
    double total_out_var = 0.0;

    for (int s = 0; s < n_samples; ++s) {
      // Random per-element perturbation
      double in_ss = 0.0;
      for (int i = 0; i < f.n; i++) {
        double noise = eps * dist(rng);
        double delta = saved[i] * noise;
        f.ptr[i] = saved[i] + delta;
        in_ss += delta * delta;
      }

      execute(dt);
      cudaDeviceSynchronize();

      double out_ss = 0.0;
      for (size_t i = 0; i < outputs.size(); ++i) {
        for (int j = 0; j < outputs[i].n; ++j) {
          double d = outputs[i].ptr[j] - baseline[i][j];
          out_ss += d * d;
        }
      }

      total_in_var += in_ss / f.n;
      total_out_var += out_ss / (out_n > 0 ? out_n : 1);

      // Restore input
      std::memcpy(f.ptr, saved.data(), f.n * sizeof(double));
    }

    double mean_in_var = total_in_var / n_samples;
    double mean_out_var = total_out_var / n_samples;
    double amplification =
        (mean_in_var > 0.0) ? std::sqrt(mean_out_var / mean_in_var) : 0.0;
    results.push_back({amplification, mean_out_var, f.name});
  }

  std::sort(results.begin(), results.end(),
            [](const auto &a, const auto &b) {
              return a.var_out > b.var_out;
            });

  std::cerr << "\n=== SENSITIVITY ANALYSIS (eps=" << std::scientific << eps
            << ", " << std::defaultfloat << n_samples << " samples) ===\n";
  std::cerr << "Measuring outputs:";
  for (const auto &o : outputs)
    std::cerr << " " << o.name;
  std::cerr << "\n";
  std::cerr << std::left << std::setw(32) << "Input field" << std::right
            << std::setw(18) << "Var(out)" << std::setw(18) << "RMS(out)\n";
  std::cerr << std::string(68, '-') << "\n";
  for (const auto &r : results)
    std::cerr << std::left << std::setw(32) << r.name << std::right
              << std::setw(18) << std::scientific << std::setprecision(4)
              << r.var_out
              << std::setw(18) << std::sqrt(r.var_out) << "\n";
  std::cerr << "=== END SENSITIVITY ANALYSIS ===\n\n";
}

inline void run_combined(const std::function<void(double)> &execute, double dt,
                         const std::vector<Field> &inputs,
                         const std::vector<Field> &outputs,
                         const std::vector<std::string> &safe_names,
                         double eps = 1e-5, int n_samples = 10) {
  // Filter inputs to only those in safe_names
  std::vector<Field> safe_inputs;
  for (const auto &f : inputs) {
    for (const auto &name : safe_names) {
      if (f.name == name) {
        safe_inputs.push_back(f);
        break;
      }
    }
  }

  // Baseline run
  execute(dt);
  cudaDeviceSynchronize();

  long long out_n = 0;
  std::vector<std::vector<double>> baseline(outputs.size());
  for (size_t i = 0; i < outputs.size(); ++i) {
    baseline[i].resize(outputs[i].n);
    std::memcpy(baseline[i].data(), outputs[i].ptr,
                outputs[i].n * sizeof(double));
    out_n += outputs[i].n;
  }

  // Save original inputs
  std::vector<std::vector<double>> saved_inputs(safe_inputs.size());
  for (size_t i = 0; i < safe_inputs.size(); ++i) {
    const auto &f = safe_inputs[i];
    if (!f.ptr || f.n <= 0)
      continue;
    saved_inputs[i].resize(f.n);
    std::memcpy(saved_inputs[i].data(), f.ptr, f.n * sizeof(double));
  }

  // Monte Carlo: random per-element perturbations, accumulate Var(output)
  std::mt19937 rng(42);
  std::uniform_real_distribution<double> dist(-1.0, 1.0);

  double total_out_var = 0.0;

  for (int s = 0; s < n_samples; ++s) {
    // Perturb all safe inputs with random noise
    for (size_t i = 0; i < safe_inputs.size(); ++i) {
      const auto &f = safe_inputs[i];
      if (!f.ptr || f.n <= 0)
        continue;
      for (int j = 0; j < f.n; j++) {
        double noise = eps * dist(rng);
        f.ptr[j] = saved_inputs[i][j] * (1.0 + noise);
      }
    }

    execute(dt);
    cudaDeviceSynchronize();

    double out_ss = 0.0;
    for (size_t i = 0; i < outputs.size(); ++i) {
      for (int j = 0; j < outputs[i].n; ++j) {
        double d = outputs[i].ptr[j] - baseline[i][j];
        out_ss += d * d;
      }
    }
    total_out_var += out_ss / (out_n > 0 ? out_n : 1);

    // Restore inputs
    for (size_t i = 0; i < safe_inputs.size(); ++i) {
      const auto &f = safe_inputs[i];
      if (!f.ptr || f.n <= 0)
        continue;
      std::memcpy(f.ptr, saved_inputs[i].data(), f.n * sizeof(double));
    }
  }

  double mean_out_var = total_out_var / n_samples;

  std::cerr << "\n=== COMBINED SENSITIVITY (" << safe_inputs.size()
            << " safe inputs, " << n_samples
            << " random samples, eps=" << std::scientific << eps << ") ===\n";
  std::cerr << "Fields:";
  for (const auto &f : safe_inputs)
    std::cerr << " " << f.name;
  std::cerr << "\n";
  std::cerr << "Var(output delta): " << std::scientific
            << std::setprecision(6) << mean_out_var << "\n";
  std::cerr << "RMS(output delta): " << std::scientific
            << std::setprecision(6) << std::sqrt(mean_out_var) << "\n";
  std::cerr << "=== END COMBINED SENSITIVITY ===\n\n";
}

} // namespace sensitivity
