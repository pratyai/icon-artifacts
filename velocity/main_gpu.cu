#include <chrono>
#include <cstring>
#include <execinfo.h>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#ifndef LOWPREC_TAG
#define LOWPREC_TAG "fp64"
#endif

#include "gpu_mem.h"
#include "sensitivity.cuh"
#include "serde_velocity_no_nproma_gpu.h"
#include "sqlite_logger.h"
#include "timer.h"
#include "velocity_tendencies_no_nproma_gpu.h"

template <typename T1, typename T2>
__global__ void integrate_state(T1 *field, const T2 *tendency, double dt,
                                int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n)
    field[i] += tendency[i] * dt;
}

template <typename T1, typename T2>
__global__ void integrate_w(T1 *w, const T2 *tendency, double dt, int nproma,
                            int nlev, int nblks) {
  int ic = blockIdx.x * blockDim.x + threadIdx.x;
  int jk = blockIdx.y * blockDim.y + threadIdx.y;
  int jb = blockIdx.z * blockDim.z + threadIdx.z;

  if (ic < nproma && jk < nlev && jb < nblks) {
    // ICON w-tendency is computed for interior levels (jk=2 to nlev in
    // Fortran). In 0-indexed C++, this maps to jk=1 to nlev-1.
    if (jk == 0)
      return;

    int w_idx = ic + jk * nproma + jb * nproma * (nlev + 1);
    int t_idx = ic + jk * nproma + jb * nproma * nlev;
    w[w_idx] += tendency[t_idx] * dt;
  }
}

template <typename T1, typename T2, typename T3>
__global__ void compute_eff_tendency(T1 *tendency, const T2 *field_final,
                                     const T3 *field_orig, double dtime,
                                     int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n)
    tendency[i] = static_cast<float>(field_final[i] - field_orig[i]) /
                  static_cast<float>(dtime);
}

template <typename T1, typename T2, typename T3>
__global__ void compute_eff_tendency_w(T1 *tendency, const T2 *w_final,
                                       const T3 *w_orig, double dtime,
                                       int nproma, int nlev, int nblks) {
  int ic = blockIdx.x * blockDim.x + threadIdx.x;
  int jk = blockIdx.y * blockDim.y + threadIdx.y;
  int jb = blockIdx.z * blockDim.z + threadIdx.z;

  if (ic < nproma && jk < nlev && jb < nblks) {
    if (jk == 0)
      return;
    int w_idx = ic + jk * nproma + jb * nproma * (nlev + 1);
    int t_idx = ic + jk * nproma + jb * nproma * nlev;
    tendency[t_idx] = static_cast<float>(w_final[w_idx] - w_orig[w_idx]) /
                      static_cast<float>(dtime);
  }
}

template <std::ostream &CS> struct AtomicStream {
  std::ostringstream s;
  template <typename T> AtomicStream &operator<<(const T &t) {
    s << t;
    return *this;
  }
  AtomicStream &operator<<(std::ostream &(*manip)(std::ostream &)) {
    s << manip;
    return *this;
  }
  ~AtomicStream() {
    static std::mutex g;
    std::lock_guard<std::mutex> lock(g);
    CS << s.str() << std::flush;
  }
};
using acout = AtomicStream<std::cout>;
using acerr = AtomicStream<std::cerr>;

template <typename F> auto spawn(std::vector<std::jthread> &pool, F &&f) {
  using R = std::invoke_result_t<F>;

  std::promise<R> prom;
  std::future<R> fut = prom.get_future();

  pool.emplace_back([p = std::move(prom), func = std::forward<F>(f)]() mutable {
    try {
      p.set_value(func());
    } catch (...) {
      p.set_exception(std::current_exception());
    }
  });
  return fut;
}

std::ifstream open_ifstream(const std::filesystem::path &ROOT,
                            const std::string &name, int timestep) {
  const std::filesystem::path datapath =
      ROOT / (name + "." + std::to_string(timestep) + ".data");
  if (!std::filesystem::exists(datapath)) {
    acerr() << "Cannot find: " << datapath << std::endl;
    exit(EXIT_FAILURE);
  }
  acout() << "Reading from: " << datapath << std::endl;
  return std::ifstream{datapath};
}

template <typename T>
std::enable_if_t<std::is_pointer_v<T>, T>
read(const std::filesystem::path &ROOT, const std::string &name, int timestep) {
  auto data = open_ifstream(ROOT, name, timestep);
  using Pointee = std::remove_pointer_t<T>;
  auto result = serde::read_array<Pointee>(data);
  auto &m = std::get<0>(result);
  auto &arr = std::get<1>(result);
  return arr;
}

template <typename T>
std::enable_if_t<std::is_class_v<T> || std::is_arithmetic_v<T>, T>
read(const std::filesystem::path &ROOT, const std::string &name, int timestep) {
  auto data = open_ifstream(ROOT, name, timestep);
  T t{};
  serde::deserialize(&t, data);
  return t;
}

template <>
global_data_type read<global_data_type>(const std::filesystem::path &ROOT,
                                        const std::string &name, int timestep) {
  auto data = open_ifstream(ROOT, name, timestep);
  global_data_type t{};
  serde::deserialize_global_data(&t, data);
  return t;
}

template <typename T>
std::pair<T, T> t0_t1_pair(const std::filesystem::path &ROOT,
                           const std::string &name, int timestep) {
  return {read<T>(ROOT, name + ".t0", timestep),
          read<T>(ROOT, name + ".t1", timestep)};
}

std::ofstream open_ofstream(const std::filesystem::path &ROOT,
                            const std::string &name, int timestep,
                            const std::string &suffix) {
  const std::filesystem::path datapath(name + "_" + std::to_string(timestep) +
                                       "." + suffix);
  acout() << "Writing to: " << ROOT / datapath << std::endl;
  return std::ofstream{ROOT / datapath};
}

template <typename T>
std::enable_if_t<std::is_pointer_v<T>, void>
got_want_pair(T got, T want, const std::string &name, int timestep,
              const std::filesystem::path &ROOT, const RunConfig &cfg) {
  std::string sgot = serde::serialize_array(got);
  std::string swant = serde::serialize_array(want);

  if (cfg.db_file.empty()) {
    std::jthread tgot([&, s = sgot] {
      open_ofstream(ROOT, name, timestep, "got") << s << std::endl;
    });
    std::jthread twant([&, s = swant] {
      open_ofstream(ROOT, name, timestep, "want") << s << std::endl;
    });
  } else {
    save_field_to_db(cfg, name, sgot.data(), sgot.size(), swant.data(),
                     swant.size());
  }
}

template <typename T>
std::enable_if_t<std::is_class_v<T> || std::is_arithmetic_v<T>, void>
got_want_pair(const T &got, const T &want, const std::string &name,
              int timestep, const std::filesystem::path &ROOT,
              const RunConfig &cfg) {
  std::string sgot = serde::serialize(&got);
  std::string swant = serde::serialize(&want);

  if (cfg.db_file.empty()) {
    std::jthread tgot([&, s = sgot] {
      open_ofstream(ROOT, name, timestep, "got") << s << std::endl;
    });
    std::jthread twant([&, s = swant] {
      open_ofstream(ROOT, name, timestep, "want") << s << std::endl;
    });
  } else {
    save_field_to_db(cfg, name, sgot.data(), sgot.size(), swant.data(),
                     swant.size());
  }
}

template <>
void got_want_pair(const global_data_type &got, const global_data_type &want,
                   const std::string &name, int timestep,
                   const std::filesystem::path &ROOT, const RunConfig &cfg) {
  std::string sgot = serde::serialize_global_data(&got);
  std::string swant = serde::serialize_global_data(&want);

  if (cfg.db_file.empty()) {
    std::jthread tgot([&, s = sgot] {
      open_ofstream(ROOT, name, timestep, "got") << s << std::endl;
    });
    std::jthread twant([&, s = swant] {
      open_ofstream(ROOT, name, timestep, "want") << s << std::endl;
    });
  } else {
    save_field_to_db(cfg, name, sgot.data(), sgot.size(), swant.data(),
                     swant.size());
  }
}

#include "flags.h"
#include <cstdlib> // for std::getenv
#include <filesystem>
#include <sstream>
#include <string>

int main(int argc, char *argv[]) {
  std::set_terminate([]() {
    void *bt[64];
    int n = backtrace(bt, 64);
    backtrace_symbols_fd(bt, n, STDERR_FILENO);
    std::abort();
  });

  flags::args args(argc, argv);

  int nproma = 20480; // default value
  if (const char *env = std::getenv("NPROMA")) {
    std::istringstream iss(env);
    int val;
    if (iss >> val && val > 0) {
      nproma = val;
    }
  }
  nproma = args.get<int>("nproma", int(nproma));

  const std::filesystem::path ROOT{
      args.get<std::string>("data", "data_nproma" + std::to_string(nproma))};
  acerr() << "Will be reading data from: " << ROOT << std::endl;
  const std::filesystem::path DUMP{args.get<std::string>(
      "gotwant",
      (std::filesystem::current_path() / "gotwant" / ROOT.filename()).string())};
  std::error_code ec;
  if (!std::filesystem::create_directories(DUMP, ec) && ec) {
    acerr() << "Failed to create directory: " << ec.message() << std::endl;
  }
  acerr() << "Will be writing got and want files to: " << DUMP << std::endl;

  std::vector<int> ns;
  if (auto ts = args.get<int>(0)) {
    ns = {*ts};
  } else {
    ns = {1, 2, 7, 9, 43, 93, 463, 519, 1140, 1814, 2593, 5701};
  }

  int reps = 20; // default value
  if (const char *env = std::getenv("REPS")) {
    std::istringstream iss(env);
    int val;
    if (iss >> val && val > 0) {
      reps = val;
    }
  }
  reps = args.get<int>("reps", int(reps));

  int substeps = 1; // default value
  if (const char *env = std::getenv("SUBSTEPS")) {
    std::istringstream iss(env);
    int val;
    if (iss >> val && val > 0) {
      substeps = val;
    }
  }
  substeps = args.get<int>("substeps", int(substeps));
  if (substeps < 1) {
    substeps = 1;
  }

  bool sensitivity_mode = args.get<bool>("sensitivity", false);
  bool skip_gotwant = args.get<bool>("skip-gotwant", false);
  double sens_eps = args.get<double>("sens-eps", 1e-5);

  // Fields validated as safe to lower (SENSITIVITY_CANDIDATES from
  // compile_gpu_stage8.py)
  const std::vector<std::string> sens_safe_names = {
      "c_lin_e",
      "cells_aw_verts",
      "coeff1_dwdz",
      "coeff2_dwdz",
      "inv_dual_edge_length",
      "inv_primal_edge_length",
      "tangent_orientation",
      "wgtfac_c",
      "ddxt_z_full",
      "ddxn_z_full",
      "ddqz_z_half",
      "area_edge",
      "area",
      "geofac_grdiv",
      "geofac_n2s",
      "w_concorr_c",
      "coeff_gradekin",
  };

  // Safe volume lookup: returns element count or 0 if ptr is null/unregistered.
  // Fields with n=0 are silently skipped by sensitivity::run.
  auto safe_vol = [](void *ptr, const char *name) -> int {
    if (!ptr) {
      std::cerr << "[sensitivity] " << name << ": null pointer, skipping\n";
      return 0;
    }
    auto *M = serde::ARRAY_META_DICT();
    auto it = M->find(ptr);
    if (it == M->end()) {
      std::cerr << "[sensitivity] " << name
                << ": not in ARRAY_META_DICT, skipping\n";
      return 0;
    }
    return it->second.volume();
  };

  std::string db_file =
      args.get<std::string>("db", args.get<std::string>(1, ""));

  int n1 = ns.size() == 1 ? ns[0] : -1;
  acout() << "Running: " << n1 << std::endl;

  for (int n : ns) {
    acerr() << "Reading data for " << n << "..." << std::endl;

    // Read input (t0) data — always needed.
    // Reference output (t1) data — only needed for got/want comparison.
    auto global_data = read<global_data_type>(ROOT, "global_data.t0", n);
    auto p_diag = read<t_nh_diag>(ROOT, "p_diag.t0", n);
    auto p_int = read<t_int_state>(ROOT, "p_int", n);
    auto p_metrics = read<t_nh_metrics>(ROOT, "p_metrics.t0", n);
    auto p_patch = read<t_patch>(ROOT, "p_patch", n);
    auto p_prog = read<t_nh_prog>(ROOT, "p_prog.t0", n);
    auto z_kin_hor_e = read<double *>(ROOT, "z_kin_hor_e.t0", n);
    auto z_vt_ie = read<double *>(ROOT, "z_vt_ie.t0", n);
    auto z_w_concorr_me = read<double *>(ROOT, "z_w_concorr_me.t0", n);

    // Want data (t1) — skip if not writing got/want.
    std::optional<global_data_type> global_data_want_storage;
    std::optional<t_nh_diag> p_diag_want_storage;
    std::optional<t_nh_metrics> p_metrics_want_storage;
    std::optional<t_nh_prog> p_prog_want_storage;
    double *z_kin_hor_e_want = nullptr;
    double *z_vt_ie_want = nullptr;
    double *z_w_concorr_me_want = nullptr;
    if (!skip_gotwant) {
      global_data_want_storage.emplace(read<global_data_type>(ROOT, "global_data.t1", n));
      p_diag_want_storage.emplace(read<t_nh_diag>(ROOT, "p_diag.t1", n));
      p_metrics_want_storage.emplace(read<t_nh_metrics>(ROOT, "p_metrics.t1", n));
      p_prog_want_storage.emplace(read<t_nh_prog>(ROOT, "p_prog.t1", n));
      z_kin_hor_e_want = read<double *>(ROOT, "z_kin_hor_e.t1", n);
      z_vt_ie_want = read<double *>(ROOT, "z_vt_ie.t1", n);
      z_w_concorr_me_want = read<double *>(ROOT, "z_w_concorr_me.t1", n);
    }

    int istep = read<int>(ROOT, "istep", n);
    int ldeepatmo = read<int>(ROOT, "ldeepatmo", n);
    int lvn_only = read<int>(ROOT, "lvn_only", n);
    int ntnd = read<int>(ROOT, "ntnd", n);
    double dt_linintp_ubc = read<double>(ROOT, "dt_linintp_ubc", n);
    double dtime = read<double>(ROOT, "dtime", n);

    RunConfig cfg{LOWPREC_TAG, substeps, n,         global_data.nproma,
                  istep,       lvn_only, ldeepatmo, db_file};
    acerr() << "All data read..." << std::endl;

    size_t total_bytes_h2d = 0;
    {
      size_t &total_bytes = total_bytes_h2d;
      auto add_field = [&](auto *ptr, const char *name,
                           size_t custom_el_size = 0) {
        if (!ptr)
          return;
        auto it = serde::ARRAY_META_DICT()->find((void *)ptr);
        if (it == serde::ARRAY_META_DICT()->end())
          return;
        size_t el_size = custom_el_size ? custom_el_size : sizeof(*ptr);
        size_t bytes = (size_t)it->second.volume() * el_size;
        total_bytes += bytes;
      };

      // Control / global
      add_field(global_data.nflatlev, "nflatlev");
      add_field(global_data.nrdmax, "nrdmax");

      // p_int
      add_field(p_int.c_lin_e, "c_lin_e");
      add_field(p_int.e_bln_c_s, "e_bln_c_s");
      add_field(p_int.cells_aw_verts, "cells_aw_verts");
      add_field(p_int.rbf_vec_coeff_e, "rbf_vec_coeff_e");
      add_field(p_int.geofac_grdiv, "geofac_grdiv");
      add_field(p_int.geofac_rot, "geofac_rot");
      add_field(p_int.geofac_n2s, "geofac_n2s");

      // p_metrics
      add_field(p_metrics.ddxn_z_full, "ddxn_z_full");
      add_field(p_metrics.ddxt_z_full, "ddxt_z_full");
      add_field(p_metrics.ddqz_z_full_e, "ddqz_z_full_e");
      add_field(p_metrics.ddqz_z_half, "ddqz_z_half");
      add_field(p_metrics.wgtfac_c, "wgtfac_c");
      add_field(p_metrics.wgtfac_e, "wgtfac_e");
      add_field(p_metrics.wgtfacq_e, "wgtfacq_e");
      add_field(p_metrics.coeff_gradekin, "coeff_gradekin");
      add_field(p_metrics.coeff1_dwdz, "coeff1_dwdz");
      add_field(p_metrics.coeff2_dwdz, "coeff2_dwdz");

      // p_patch
      if (p_patch.cells) {
        add_field(p_patch.cells->neighbor_idx, "neighbor_idx");
        add_field(p_patch.cells->neighbor_blk, "neighbor_blk");
        add_field(p_patch.cells->edge_idx, "c_edge_idx");
        add_field(p_patch.cells->edge_blk, "c_edge_blk");
        add_field(p_patch.cells->area, "c_area");
        add_field(p_patch.cells->start_index, "c_start_index");
        add_field(p_patch.cells->end_index, "c_end_index");
        add_field(p_patch.cells->start_block, "c_start_block");
        add_field(p_patch.cells->end_block, "c_end_block");
        if (p_patch.cells->decomp_info)
          add_field(p_patch.cells->decomp_info->owner_mask, "owner_mask");
      }
      if (p_patch.edges) {
        add_field(p_patch.edges->cell_idx, "e_cell_idx");
        add_field(p_patch.edges->cell_blk, "e_cell_blk");
        add_field(p_patch.edges->vertex_idx, "e_vertex_idx");
        add_field(p_patch.edges->vertex_blk, "e_vertex_blk");
        add_field(p_patch.edges->tangent_orientation, "tangent_orientation");
        add_field(p_patch.edges->quad_idx, "quad_idx");
        add_field(p_patch.edges->quad_blk, "quad_blk");
        add_field(p_patch.edges->inv_primal_edge_length,
                  "inv_primal_edge_length");
        add_field(p_patch.edges->inv_dual_edge_length, "inv_dual_edge_length");
        add_field(p_patch.edges->area_edge, "area_edge");
        add_field(p_patch.edges->f_e, "f_e");
        add_field(p_patch.edges->start_index, "e_start_index");
        add_field(p_patch.edges->end_index, "e_end_index");
        add_field(p_patch.edges->start_block, "e_start_block");
        add_field(p_patch.edges->end_block, "e_end_block");
      }
      if (p_patch.verts) {
        add_field(p_patch.verts->cell_idx, "v_cell_idx");
        add_field(p_patch.verts->cell_blk, "v_cell_blk");
        add_field(p_patch.verts->edge_idx, "v_edge_idx");
        add_field(p_patch.verts->edge_blk, "v_edge_blk");
        add_field(p_patch.verts->start_index, "v_start_index");
        add_field(p_patch.verts->end_index, "v_end_index");
        add_field(p_patch.verts->start_block, "v_start_block");
        add_field(p_patch.verts->end_block, "v_end_block");
      }

      // p_prog
      add_field(p_prog.vn, "vn");
      add_field(p_prog.w, "w");

      // p_diag
      add_field(p_diag.vt, "vt");
      add_field(p_diag.vn_ie, "vn_ie");
      add_field(p_diag.w_concorr_c, "w_concorr_c");
      add_field(p_diag.ddt_vn_apc_pc, "ddt_vn_apc_pc");
      add_field(p_diag.ddt_w_adv_pc, "ddt_w_adv_pc");

      // Standalone intermediates (Use their target precision types)
      add_field(z_kin_hor_e, "z_kin_hor_e", sizeof(z_kin_hor_e_t));
      add_field(z_vt_ie, "z_vt_ie", sizeof(z_vt_ie_t));
      add_field(z_w_concorr_me, "z_w_concorr_me", sizeof(z_w_concorr_me_t));

      acout() << "GPU Bytes Transferred: " << total_bytes << " ("
              << (double)total_bytes / 1024 / 1024 << " MB)" << std::endl;
    }

    // Working buffers at target precision for the three standalone z_
    // parameters. The fp64 source pointers remain valid for meta-dict lookups
    // and got/want.
    int z_kin_n = serde::ARRAY_META_DICT()->at(z_kin_hor_e).volume();
    int z_vt_n = serde::ARRAY_META_DICT()->at(z_vt_ie).volume();
    int z_w_n = serde::ARRAY_META_DICT()->at(z_w_concorr_me).volume();

    std::vector<z_kin_hor_e_t> z_kin_hor_e_lp(z_kin_n);
    std::vector<z_vt_ie_t> z_vt_ie_lp(z_vt_n);
    std::vector<z_w_concorr_me_t> z_w_concorr_me_lp(z_w_n);

    std::copy(z_kin_hor_e, z_kin_hor_e + z_kin_n, z_kin_hor_e_lp.begin());
    std::copy(z_vt_ie, z_vt_ie + z_vt_n, z_vt_ie_lp.begin());
    std::copy(z_w_concorr_me, z_w_concorr_me + z_w_n,
              z_w_concorr_me_lp.begin());

    if (ldeepatmo != 0) {
      throw std::runtime_error("ldeepatmo is not 0");
    }
    if (global_data.lextra_diffu != 1) {
      throw std::runtime_error("lextra_diffu is not 1");
    }
    if (istep != 1 && istep != 2) {
      throw std::runtime_error("istep not 1 or 2");
    }
    if (lvn_only != 0 && lvn_only != 1) {
      throw std::runtime_error("lvn_only not 0 or 1");
    }
    acout() << "Step " << n
            << " variables, extra_diffu: " << global_data.lextra_diffu
            << ", istep: ";
    acout() << istep << ", lvn_only: " << lvn_only
            << ", ldeepatmo: " << ldeepatmo << ", substeps: " << substeps
            << std::endl;

    size_t free_bytes_start, total_bytes_start;
    cudaMemGetInfo(&free_bytes_start, &total_bytes_start);
    acout() << "Baseline Real GPU Memory: "
            << (double)(total_bytes_start - free_bytes_start) / 1024 / 1024
            << " MB used" << std::endl;

    if (lvn_only == 1 && istep == 1) {
      auto *h = __dace_init_velocity_no_nproma_if_prop_lvn_only_1_istep_1(
          &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
          z_kin_hor_e_lp.data(), z_vt_ie_lp.data(), z_w_concorr_me_lp.data(),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(0),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(1),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(2),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(0),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(1),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(2),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(0),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(1),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(2),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(2),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(2),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(2),
          dt_linintp_ubc, dtime, istep, ldeepatmo, lvn_only, ntnd);

      size_t free_bytes_now, total_bytes_now;
      cudaMemGetInfo(&free_bytes_now, &total_bytes_now);
      acout() << "Internal GPU Bytes Allocated: " << get_gpu_mem_usage() << " ("
              << (double)get_gpu_mem_usage() / 1024 / 1024 << " MB)"
              << std::endl;
      acout() << "Real GPU Memory after init: "
              << (double)(total_bytes_now - free_bytes_now) / 1024 / 1024
              << " MB used" << std::endl;
      acout() << "Residency Increase: "
              << (double)(free_bytes_start - free_bytes_now) / 1024 / 1024
              << " MB" << std::endl;
      save_gpu_mem_to_db(cfg, total_bytes_h2d, get_gpu_mem_usage(),
                         total_bytes_now - free_bytes_now,
                         free_bytes_start - free_bytes_now);

      auto execute = [&](double dt) {
        __program_velocity_no_nproma_if_prop_lvn_only_1_istep_1(
            h, &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
            z_kin_hor_e_lp.data(), z_vt_ie_lp.data(), z_w_concorr_me_lp.data(),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(0),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(1),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(2),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(0),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(1),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(2),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(0),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(1),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(2),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(2),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(2),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(2),
            dt_linintp_ubc, dt, istep, ldeepatmo, lvn_only, ntnd);
      };

      // Determine array sizes for integration and backup
      auto &meta = *serde::ARRAY_META_DICT();
      auto get_total_size = [](const serde::array_meta &m) {
        int total = 1;
        for (int s : m.size)
          total *= s;
        return total;
      };

      int vn_size = get_total_size(meta.at(p_prog.vn));
      int w_full_size = get_total_size(meta.at(p_prog.w));
      int nproma_val = meta.at(p_prog.w).size.at(0);
      int nlevp1_val = meta.at(p_prog.w).size.at(1);
      int nlev_val = nlevp1_val - 1;
      int nblks_val = meta.at(p_prog.w).size.at(2);
      int w_tend_slice_size = nproma_val * nlev_val * nblks_val;

      double small_dt = dtime / substeps;
      int actual_reps = (substeps > 1) ? 1 : reps;

      // Check DaCe-managed transients if they are accessible
      // (Note: This depends on if they are in the __state or passed as
      // arguments)

      using VnElem = std::remove_pointer_t<decltype(p_prog.vn)>;
      using WElem = std::remove_pointer_t<decltype(p_prog.w)>;
      const size_t vn_bytes = vn_size * sizeof(VnElem);
      const size_t w_bytes = w_full_size * sizeof(WElem);
      VnElem *vn_backup = new VnElem[vn_size];
      WElem *w_backup = new WElem[w_full_size];
      std::memcpy(vn_backup, p_prog.vn, vn_bytes);
      std::memcpy(w_backup, p_prog.w, w_bytes);

      if (sensitivity_mode) {
        std::vector<sensitivity::Field> sens_in = {
            // FP64 prognostics — not reduction candidates
            // {p_prog.vn, vn_size, "vn"},
            // {p_prog.exner, meta.at(p_prog.exner).volume(), "exner"},
            // {p_prog.rho, meta.at(p_prog.rho).volume(), "rho"},
            // {p_prog.theta_v, meta.at(p_prog.theta_v).volume(), "theta_v"},
            // Interpolation coefficients (LOW_PREC_TYPE)
            {p_int.c_lin_e, safe_vol(p_int.c_lin_e, "c_lin_e"), "c_lin_e"},
            {p_int.e_bln_c_s, safe_vol(p_int.e_bln_c_s, "e_bln_c_s"),
             "e_bln_c_s"},
            {p_int.geofac_grdiv, safe_vol(p_int.geofac_grdiv, "geofac_grdiv"),
             "geofac_grdiv"},
            {p_int.geofac_rot, safe_vol(p_int.geofac_rot, "geofac_rot"),
             "geofac_rot"},
            {p_int.rbf_vec_coeff_e,
             safe_vol(p_int.rbf_vec_coeff_e, "rbf_vec_coeff_e"),
             "rbf_vec_coeff_e"},
            // Metrics (LOW_PREC_TYPE; ddqz_z_full_e hardcoded double — skipped)
            {p_metrics.ddqz_z_half,
             safe_vol(p_metrics.ddqz_z_half, "ddqz_z_half"), "ddqz_z_half"},
            {p_metrics.wgtfac_e, safe_vol(p_metrics.wgtfac_e, "wgtfac_e"),
             "wgtfac_e"},
            {p_metrics.wgtfac_c, safe_vol(p_metrics.wgtfac_c, "wgtfac_c"),
             "wgtfac_c"},
            {p_metrics.coeff1_dwdz,
             safe_vol(p_metrics.coeff1_dwdz, "coeff1_dwdz"), "coeff1_dwdz"},
            {p_metrics.coeff2_dwdz,
             safe_vol(p_metrics.coeff2_dwdz, "coeff2_dwdz"), "coeff2_dwdz"},
            {p_metrics.ddxn_z_full,
             safe_vol(p_metrics.ddxn_z_full, "ddxn_z_full"), "ddxn_z_full"},
            {p_metrics.ddxt_z_full,
             safe_vol(p_metrics.ddxt_z_full, "ddxt_z_full"), "ddxt_z_full"},
            // inv_ddqz_z_full: not used in any SDFG variant — skip
            // {p_metrics.inv_ddqz_z_full, ..., "inv_ddqz_z_full"},
            // Edges (LOW_PREC_TYPE)
            {p_patch.edges ? p_patch.edges->f_e : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->f_e : nullptr, "f_e"),
             "f_e"},
            {p_patch.edges ? p_patch.edges->area_edge : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->area_edge : nullptr,
                      "area_edge"),
             "area_edge"},
            // --- Untested fields from hardcoded lowering lists ---
            // GRID_METRICS
            {p_patch.edges ? p_patch.edges->inv_dual_edge_length : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->inv_dual_edge_length
                                    : nullptr,
                      "inv_dual_edge_length"),
             "inv_dual_edge_length"},
            {p_patch.edges ? p_patch.edges->inv_primal_edge_length : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->inv_primal_edge_length
                                    : nullptr,
                      "inv_primal_edge_length"),
             "inv_primal_edge_length"},
            {p_patch.cells ? p_patch.cells->area : nullptr,
             safe_vol(p_patch.cells ? p_patch.cells->area : nullptr, "area"),
             "area"},
            {p_metrics.coeff_gradekin,
             safe_vol(p_metrics.coeff_gradekin, "coeff_gradekin"),
             "coeff_gradekin"},
            {p_metrics.wgtfacq_e, safe_vol(p_metrics.wgtfacq_e, "wgtfacq_e"),
             "wgtfacq_e"},
            // INTERPOLATION_COEFFS
            {p_int.geofac_n2s, safe_vol(p_int.geofac_n2s, "geofac_n2s"),
             "geofac_n2s"},
            {p_int.cells_aw_verts,
             safe_vol(p_int.cells_aw_verts, "cells_aw_verts"),
             "cells_aw_verts"},
            {p_patch.edges ? p_patch.edges->tangent_orientation : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->tangent_orientation
                                    : nullptr,
                      "tangent_orientation"),
             "tangent_orientation"},
            // vt/vn_ie/w_concorr_c: outputs of istep==1, not inputs
        };
        std::vector<sensitivity::Field> sens_out = {
            {p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, vn_size,
             "ddt_vn_apc_pc"},
            {p_diag.vt, safe_vol(p_diag.vt, "vt"), "vt"},
            {p_diag.vn_ie, safe_vol(p_diag.vn_ie, "vn_ie"), "vn_ie"},
            {p_diag.w_concorr_c, safe_vol(p_diag.w_concorr_c, "w_concorr_c"),
             "w_concorr_c"},
            {z_kin_hor_e, safe_vol(z_kin_hor_e, "z_kin_hor_e"), "z_kin_hor_e"},
            {z_vt_ie, safe_vol(z_vt_ie, "z_vt_ie"), "z_vt_ie"},
            {z_w_concorr_me, safe_vol(z_w_concorr_me, "z_w_concorr_me"),
             "z_w_concorr_me"},
        };
        auto execute_sens = [&](double dt) {
          std::memcpy(p_prog.vn, vn_backup, vn_bytes);
          std::memcpy(p_prog.w, w_backup, w_bytes);
          execute(dt);
          // Sync lp → fp64 so sensitivity::run can read z_ outputs as double.
          for (int i = 0; i < z_kin_n; i++)
            z_kin_hor_e[i] = z_kin_hor_e_lp[i];
          for (int i = 0; i < z_vt_n; i++)
            z_vt_ie[i] = z_vt_ie_lp[i];
          for (int i = 0; i < z_w_n; i++)
            z_w_concorr_me[i] = z_w_concorr_me_lp[i];
        };
        sensitivity::run(execute_sens, dtime, sens_in, sens_out, sens_eps);
        sensitivity::run_combined(execute_sens, dtime, sens_in, sens_out,
                                  sens_safe_names, sens_eps);
      } else {
        for (int r = 0; r < actual_reps; r++) {
          if (actual_reps > 1)
            std::cout << "Timing iteration: " << r << std::endl;
          std::memcpy(p_prog.vn, vn_backup, vn_bytes);
          std::memcpy(p_prog.w, w_backup, w_bytes);

          drain_timing_results();

          Capture cap;
          for (int s = 0; s < substeps; s++) {
            execute(small_dt);
            if (s == 0 && substeps > 1) {
              cudaDeviceSynchronize();
              cap.add(&p_diag.max_vcfl_dyn, sizeof(double), (double)substeps);
              cap.add(z_kin_hor_e_lp.data(), z_kin_n * sizeof(z_kin_hor_e_t));
              cap.add(z_vt_ie_lp.data(), z_vt_n * sizeof(z_vt_ie_t));
              cap.add(z_w_concorr_me_lp.data(),
                      z_w_n * sizeof(z_w_concorr_me_t));
              cap.add(p_diag.vt,
                      meta.at(p_diag.vt).volume() * sizeof(*p_diag.vt));
              cap.add(p_diag.vn_ie,
                      meta.at(p_diag.vn_ie).volume() * sizeof(*p_diag.vn_ie));
              cap.add(p_diag.w_concorr_c, meta.at(p_diag.w_concorr_c).volume() *
                                              sizeof(*p_diag.w_concorr_c));
            }
            if (substeps > 1) {
              int threads = 256;
              integrate_state<<<(vn_size + threads - 1) / threads, threads>>>(
                  p_prog.vn, p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size,
                  small_dt, vn_size);
              if (!lvn_only) {
                dim3 block(32, 8, 1);
                dim3 grid((nproma_val + block.x - 1) / block.x,
                          (nlev_val + block.y - 1) / block.y,
                          (nblks_val + block.z - 1) / block.z);
                integrate_w<<<grid, block>>>(
                    p_prog.w,
                    p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
                    small_dt, nproma_val, nlev_val, nblks_val);
              }
            }
            cudaDeviceSynchronize();
          }
          cap.restore();

          if (substeps > 1) {

            int threads = 256;
            compute_eff_tendency<<<(vn_size + threads - 1) / threads,
                                   threads>>>(
                p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, p_prog.vn,
                vn_backup, dtime, vn_size);
            if (!lvn_only) {
              dim3 block(32, 8, 1);
              dim3 grid((nproma_val + block.x - 1) / block.x,
                        (nlev_val + block.y - 1) / block.y,
                        (nblks_val + block.z - 1) / block.z);
              compute_eff_tendency_w<<<grid, block>>>(
                  p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
                  p_prog.w, w_backup, dtime, nproma_val, nlev_val, nblks_val);
            }
            cudaDeviceSynchronize();
          }
          auto results = drain_timing_results();
          for (const auto &tr : results)
            save_timing_to_db(cfg, r, tr.tag, tr.time_us);
        }

        std::memcpy(p_prog.vn, vn_backup, vn_bytes);
        std::memcpy(p_prog.w, w_backup, w_bytes);
      }

      delete[] vn_backup;
      delete[] w_backup;

      __dace_exit_velocity_no_nproma_if_prop_lvn_only_1_istep_1(h);

    } else if (lvn_only == 0 && istep == 1) {
      auto *h = __dace_init_velocity_no_nproma_if_prop_lvn_only_0_istep_1(
          &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
          z_kin_hor_e_lp.data(), z_vt_ie_lp.data(), z_w_concorr_me_lp.data(),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(0),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(1),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(2),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(0),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(1),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(2),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(0),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(1),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(2),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(2),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(2),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(2),
          dt_linintp_ubc, dtime, istep, ldeepatmo, lvn_only, ntnd);

      size_t free_bytes_now, total_bytes_now;
      cudaMemGetInfo(&free_bytes_now, &total_bytes_now);
      acout() << "Internal GPU Bytes Allocated: " << get_gpu_mem_usage() << " ("
              << (double)get_gpu_mem_usage() / 1024 / 1024 << " MB)"
              << std::endl;
      acout() << "Real GPU Memory after init: "
              << (double)(total_bytes_now - free_bytes_now) / 1024 / 1024
              << " MB used" << std::endl;
      acout() << "Residency Increase: "
              << (double)(free_bytes_start - free_bytes_now) / 1024 / 1024
              << " MB" << std::endl;
      save_gpu_mem_to_db(cfg, total_bytes_h2d, get_gpu_mem_usage(),
                         total_bytes_now - free_bytes_now,
                         free_bytes_start - free_bytes_now);

      auto execute = [&](double dt) {
        __program_velocity_no_nproma_if_prop_lvn_only_0_istep_1(
            h, &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
            z_kin_hor_e_lp.data(), z_vt_ie_lp.data(), z_w_concorr_me_lp.data(),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(0),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(1),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(2),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(0),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(1),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(2),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(0),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(1),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(2),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(2),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(2),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).lbound.at(2),
            dt_linintp_ubc, dt, istep, ldeepatmo, lvn_only, ntnd);
      };

      // Determine array sizes for integration and backup
      auto &meta = *serde::ARRAY_META_DICT();
      auto get_total_size = [](const serde::array_meta &m) {
        int total = 1;
        for (int s : m.size)
          total *= s;
        return total;
      };

      int vn_size = get_total_size(meta.at(p_prog.vn));
      int w_full_size = get_total_size(meta.at(p_prog.w));
      int nproma_val = meta.at(p_prog.w).size.at(0);
      int nlevp1_val = meta.at(p_prog.w).size.at(1);
      int nlev_val = nlevp1_val - 1;
      int nblks_val = meta.at(p_prog.w).size.at(2);
      int w_tend_slice_size = nproma_val * nlev_val * nblks_val;

      double small_dt = dtime / substeps;
      int actual_reps = (substeps > 1) ? 1 : reps;

      // Check DaCe-managed transients if they are accessible
      // (Note: This depends on if they are in the __state or passed as
      // arguments)

      using VnElem = std::remove_pointer_t<decltype(p_prog.vn)>;
      using WElem = std::remove_pointer_t<decltype(p_prog.w)>;
      const size_t vn_bytes = vn_size * sizeof(VnElem);
      const size_t w_bytes = w_full_size * sizeof(WElem);
      VnElem *vn_backup = new VnElem[vn_size];
      WElem *w_backup = new WElem[w_full_size];
      std::memcpy(vn_backup, p_prog.vn, vn_bytes);
      std::memcpy(w_backup, p_prog.w, w_bytes);

      if (sensitivity_mode) {
        std::vector<sensitivity::Field> sens_in = {
            // FP64 prognostics — not reduction candidates
            // {p_prog.vn, vn_size, "vn"},
            // {p_prog.w, w_full_size, "w"},
            // {p_prog.exner, meta.at(p_prog.exner).volume(), "exner"},
            // {p_prog.rho, meta.at(p_prog.rho).volume(), "rho"},
            // {p_prog.theta_v, meta.at(p_prog.theta_v).volume(), "theta_v"},
            // Interpolation coefficients (LOW_PREC_TYPE)
            {p_int.c_lin_e, safe_vol(p_int.c_lin_e, "c_lin_e"), "c_lin_e"},
            {p_int.e_bln_c_s, safe_vol(p_int.e_bln_c_s, "e_bln_c_s"),
             "e_bln_c_s"},
            {p_int.geofac_grdiv, safe_vol(p_int.geofac_grdiv, "geofac_grdiv"),
             "geofac_grdiv"},
            {p_int.geofac_rot, safe_vol(p_int.geofac_rot, "geofac_rot"),
             "geofac_rot"},
            {p_int.rbf_vec_coeff_e,
             safe_vol(p_int.rbf_vec_coeff_e, "rbf_vec_coeff_e"),
             "rbf_vec_coeff_e"},
            // Metrics (LOW_PREC_TYPE; ddqz_z_full_e hardcoded double — skipped)
            {p_metrics.ddqz_z_half,
             safe_vol(p_metrics.ddqz_z_half, "ddqz_z_half"), "ddqz_z_half"},
            {p_metrics.wgtfac_e, safe_vol(p_metrics.wgtfac_e, "wgtfac_e"),
             "wgtfac_e"},
            {p_metrics.wgtfac_c, safe_vol(p_metrics.wgtfac_c, "wgtfac_c"),
             "wgtfac_c"},
            {p_metrics.coeff1_dwdz,
             safe_vol(p_metrics.coeff1_dwdz, "coeff1_dwdz"), "coeff1_dwdz"},
            {p_metrics.coeff2_dwdz,
             safe_vol(p_metrics.coeff2_dwdz, "coeff2_dwdz"), "coeff2_dwdz"},
            {p_metrics.ddxn_z_full,
             safe_vol(p_metrics.ddxn_z_full, "ddxn_z_full"), "ddxn_z_full"},
            {p_metrics.ddxt_z_full,
             safe_vol(p_metrics.ddxt_z_full, "ddxt_z_full"), "ddxt_z_full"},
            // inv_ddqz_z_full: not used in any SDFG variant — skip
            // {p_metrics.inv_ddqz_z_full, ..., "inv_ddqz_z_full"},
            // Edges (LOW_PREC_TYPE)
            {p_patch.edges ? p_patch.edges->f_e : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->f_e : nullptr, "f_e"),
             "f_e"},
            {p_patch.edges ? p_patch.edges->area_edge : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->area_edge : nullptr,
                      "area_edge"),
             "area_edge"},
            // --- Untested fields from hardcoded lowering lists ---
            // GRID_METRICS
            {p_patch.edges ? p_patch.edges->inv_dual_edge_length : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->inv_dual_edge_length
                                    : nullptr,
                      "inv_dual_edge_length"),
             "inv_dual_edge_length"},
            {p_patch.edges ? p_patch.edges->inv_primal_edge_length : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->inv_primal_edge_length
                                    : nullptr,
                      "inv_primal_edge_length"),
             "inv_primal_edge_length"},
            {p_patch.cells ? p_patch.cells->area : nullptr,
             safe_vol(p_patch.cells ? p_patch.cells->area : nullptr, "area"),
             "area"},
            {p_metrics.coeff_gradekin,
             safe_vol(p_metrics.coeff_gradekin, "coeff_gradekin"),
             "coeff_gradekin"},
            {p_metrics.wgtfacq_e, safe_vol(p_metrics.wgtfacq_e, "wgtfacq_e"),
             "wgtfacq_e"},
            // INTERPOLATION_COEFFS
            {p_int.geofac_n2s, safe_vol(p_int.geofac_n2s, "geofac_n2s"),
             "geofac_n2s"},
            {p_int.cells_aw_verts,
             safe_vol(p_int.cells_aw_verts, "cells_aw_verts"),
             "cells_aw_verts"},
            {p_patch.edges ? p_patch.edges->tangent_orientation : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->tangent_orientation
                                    : nullptr,
                      "tangent_orientation"),
             "tangent_orientation"},
            // vt/vn_ie/w_concorr_c: outputs of istep==1, not inputs
        };
        std::vector<sensitivity::Field> sens_out = {
            {p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, vn_size,
             "ddt_vn_apc_pc"},
            {p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
             w_tend_slice_size, "ddt_w_adv_pc"},
            {p_diag.vt, safe_vol(p_diag.vt, "vt"), "vt"},
            {p_diag.vn_ie, safe_vol(p_diag.vn_ie, "vn_ie"), "vn_ie"},
            {p_diag.w_concorr_c, safe_vol(p_diag.w_concorr_c, "w_concorr_c"),
             "w_concorr_c"},
            {z_kin_hor_e, safe_vol(z_kin_hor_e, "z_kin_hor_e"), "z_kin_hor_e"},
            {z_vt_ie, safe_vol(z_vt_ie, "z_vt_ie"), "z_vt_ie"},
            {z_w_concorr_me, safe_vol(z_w_concorr_me, "z_w_concorr_me"),
             "z_w_concorr_me"},
        };
        auto execute_sens = [&](double dt) {
          std::memcpy(p_prog.vn, vn_backup, vn_bytes);
          std::memcpy(p_prog.w, w_backup, w_bytes);
          execute(dt);
          // Sync lp → fp64 so sensitivity::run can read z_ outputs as double.
          for (int i = 0; i < z_kin_n; i++)
            z_kin_hor_e[i] = z_kin_hor_e_lp[i];
          for (int i = 0; i < z_vt_n; i++)
            z_vt_ie[i] = z_vt_ie_lp[i];
          for (int i = 0; i < z_w_n; i++)
            z_w_concorr_me[i] = z_w_concorr_me_lp[i];
        };
        sensitivity::run(execute_sens, dtime, sens_in, sens_out, sens_eps);
        sensitivity::run_combined(execute_sens, dtime, sens_in, sens_out,
                                  sens_safe_names, sens_eps);
      } else {
        for (int r = 0; r < actual_reps; r++) {
          if (actual_reps > 1)
            std::cout << "Timing iteration: " << r << std::endl;
          std::memcpy(p_prog.vn, vn_backup, vn_bytes);
          std::memcpy(p_prog.w, w_backup, w_bytes);

          drain_timing_results();

          Capture cap;
          for (int s = 0; s < substeps; s++) {
            execute(small_dt);
            if (s == 0 && substeps > 1) {
              cudaDeviceSynchronize();
              cap.add(&p_diag.max_vcfl_dyn, sizeof(double), (double)substeps);
              cap.add(z_kin_hor_e_lp.data(), z_kin_n * sizeof(z_kin_hor_e_t));
              cap.add(z_vt_ie_lp.data(), z_vt_n * sizeof(z_vt_ie_t));
              cap.add(z_w_concorr_me_lp.data(),
                      z_w_n * sizeof(z_w_concorr_me_t));
              cap.add(p_diag.vt,
                      meta.at(p_diag.vt).volume() * sizeof(*p_diag.vt));
              cap.add(p_diag.vn_ie,
                      meta.at(p_diag.vn_ie).volume() * sizeof(*p_diag.vn_ie));
              cap.add(p_diag.w_concorr_c, meta.at(p_diag.w_concorr_c).volume() *
                                              sizeof(*p_diag.w_concorr_c));
            }
            if (substeps > 1) {
              int threads = 256;
              integrate_state<<<(vn_size + threads - 1) / threads, threads>>>(
                  p_prog.vn, p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size,
                  small_dt, vn_size);
              if (!lvn_only) {
                dim3 block(32, 8, 1);
                dim3 grid((nproma_val + block.x - 1) / block.x,
                          (nlev_val + block.y - 1) / block.y,
                          (nblks_val + block.z - 1) / block.z);
                integrate_w<<<grid, block>>>(
                    p_prog.w,
                    p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
                    small_dt, nproma_val, nlev_val, nblks_val);
              }
            }
            cudaDeviceSynchronize();
          }
          cap.restore();

          if (substeps > 1) {

            int threads = 256;
            compute_eff_tendency<<<(vn_size + threads - 1) / threads,
                                   threads>>>(
                p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, p_prog.vn,
                vn_backup, dtime, vn_size);
            if (!lvn_only) {
              dim3 block(32, 8, 1);
              dim3 grid((nproma_val + block.x - 1) / block.x,
                        (nlev_val + block.y - 1) / block.y,
                        (nblks_val + block.z - 1) / block.z);
              compute_eff_tendency_w<<<grid, block>>>(
                  p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
                  p_prog.w, w_backup, dtime, nproma_val, nlev_val, nblks_val);
            }
            cudaDeviceSynchronize();
          }
          auto results = drain_timing_results();
          for (const auto &tr : results)
            save_timing_to_db(cfg, r, tr.tag, tr.time_us);
        }

        std::memcpy(p_prog.vn, vn_backup, vn_bytes);
        std::memcpy(p_prog.w, w_backup, w_bytes);
      }

      delete[] vn_backup;
      delete[] w_backup;

      __dace_exit_velocity_no_nproma_if_prop_lvn_only_0_istep_1(h);

    } else if (lvn_only == 1 && istep == 2) {
      auto *h = __dace_init_velocity_no_nproma_if_prop_lvn_only_1_istep_2(
          &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
          z_kin_hor_e_lp.data(), z_vt_ie_lp.data(), z_w_concorr_me_lp.data(),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(0),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(1),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(2),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(0),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(1),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(2),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(0),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(1),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(2),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(2),
          dt_linintp_ubc, dtime, istep, ldeepatmo, lvn_only, ntnd);

      size_t free_bytes_now, total_bytes_now;
      cudaMemGetInfo(&free_bytes_now, &total_bytes_now);
      acout() << "Internal GPU Bytes Allocated: " << get_gpu_mem_usage() << " ("
              << (double)get_gpu_mem_usage() / 1024 / 1024 << " MB)"
              << std::endl;
      acout() << "Real GPU Memory after init: "
              << (double)(total_bytes_now - free_bytes_now) / 1024 / 1024
              << " MB used" << std::endl;
      acout() << "Residency Increase: "
              << (double)(free_bytes_start - free_bytes_now) / 1024 / 1024
              << " MB" << std::endl;
      save_gpu_mem_to_db(cfg, total_bytes_h2d, get_gpu_mem_usage(),
                         total_bytes_now - free_bytes_now,
                         free_bytes_start - free_bytes_now);

      auto execute = [&](double dt) {
        __program_velocity_no_nproma_if_prop_lvn_only_1_istep_2(
            h, &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
            z_kin_hor_e_lp.data(), z_vt_ie_lp.data(), z_w_concorr_me_lp.data(),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(0),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(1),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(2),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(0),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(1),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(2),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(0),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(1),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(2),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(2),
            dt_linintp_ubc, dt, istep, ldeepatmo, lvn_only, ntnd);
      };

      // Determine array sizes for integration and backup
      auto &meta = *serde::ARRAY_META_DICT();
      auto get_total_size = [](const serde::array_meta &m) {
        int total = 1;
        for (int s : m.size)
          total *= s;
        return total;
      };

      int vn_size = get_total_size(meta.at(p_prog.vn));
      int w_full_size = get_total_size(meta.at(p_prog.w));
      int nproma_val = meta.at(p_prog.w).size.at(0);
      int nlevp1_val = meta.at(p_prog.w).size.at(1);
      int nlev_val = nlevp1_val - 1;
      int nblks_val = meta.at(p_prog.w).size.at(2);
      int w_tend_slice_size = nproma_val * nlev_val * nblks_val;

      double small_dt = dtime / substeps;
      int actual_reps = (substeps > 1) ? 1 : reps;

      // Check DaCe-managed transients if they are accessible
      // (Note: This depends on if they are in the __state or passed as
      // arguments)

      using VnElem = std::remove_pointer_t<decltype(p_prog.vn)>;
      using WElem = std::remove_pointer_t<decltype(p_prog.w)>;
      const size_t vn_bytes = vn_size * sizeof(VnElem);
      const size_t w_bytes = w_full_size * sizeof(WElem);
      VnElem *vn_backup = new VnElem[vn_size];
      WElem *w_backup = new WElem[w_full_size];
      std::memcpy(vn_backup, p_prog.vn, vn_bytes);
      std::memcpy(w_backup, p_prog.w, w_bytes);

      if (sensitivity_mode) {
        std::vector<sensitivity::Field> sens_in = {
            // FP64 prognostics — not reduction candidates
            // {p_prog.vn, vn_size, "vn"},
            // {p_prog.exner, meta.at(p_prog.exner).volume(), "exner"},
            // {p_prog.rho, meta.at(p_prog.rho).volume(), "rho"},
            // {p_prog.theta_v, meta.at(p_prog.theta_v).volume(), "theta_v"},
            // Intermediates from istep==1: hardcoded double — not candidates
            // {z_kin_hor_e, meta.at(z_kin_hor_e).volume(), "z_kin_hor_e"},
            // {p_diag.vt, meta.at(p_diag.vt).volume(), "vt"},
            // {p_diag.vn_ie, meta.at(p_diag.vn_ie).volume(), "vn_ie"},
            // w_concorr_c: LOW_PREC_TYPE input from istep==1 — candidate
            {p_diag.w_concorr_c, safe_vol(p_diag.w_concorr_c, "w_concorr_c"),
             "w_concorr_c"},
            // Interpolation coefficients (LOW_PREC_TYPE)
            {p_int.c_lin_e, safe_vol(p_int.c_lin_e, "c_lin_e"), "c_lin_e"},
            {p_int.e_bln_c_s, safe_vol(p_int.e_bln_c_s, "e_bln_c_s"),
             "e_bln_c_s"},
            {p_int.geofac_grdiv, safe_vol(p_int.geofac_grdiv, "geofac_grdiv"),
             "geofac_grdiv"},
            {p_int.geofac_rot, safe_vol(p_int.geofac_rot, "geofac_rot"),
             "geofac_rot"},
            {p_int.rbf_vec_coeff_e,
             safe_vol(p_int.rbf_vec_coeff_e, "rbf_vec_coeff_e"),
             "rbf_vec_coeff_e"},
            // Metrics (LOW_PREC_TYPE; ddqz_z_full_e hardcoded double — skipped)
            {p_metrics.ddqz_z_half,
             safe_vol(p_metrics.ddqz_z_half, "ddqz_z_half"), "ddqz_z_half"},
            {p_metrics.wgtfac_e, safe_vol(p_metrics.wgtfac_e, "wgtfac_e"),
             "wgtfac_e"},
            {p_metrics.wgtfac_c, safe_vol(p_metrics.wgtfac_c, "wgtfac_c"),
             "wgtfac_c"},
            {p_metrics.coeff1_dwdz,
             safe_vol(p_metrics.coeff1_dwdz, "coeff1_dwdz"), "coeff1_dwdz"},
            {p_metrics.coeff2_dwdz,
             safe_vol(p_metrics.coeff2_dwdz, "coeff2_dwdz"), "coeff2_dwdz"},
            {p_metrics.ddxn_z_full,
             safe_vol(p_metrics.ddxn_z_full, "ddxn_z_full"), "ddxn_z_full"},
            {p_metrics.ddxt_z_full,
             safe_vol(p_metrics.ddxt_z_full, "ddxt_z_full"), "ddxt_z_full"},
            // inv_ddqz_z_full: not used in any SDFG variant — skip
            // {p_metrics.inv_ddqz_z_full, ..., "inv_ddqz_z_full"},
            // Edges (LOW_PREC_TYPE)
            {p_patch.edges ? p_patch.edges->f_e : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->f_e : nullptr, "f_e"),
             "f_e"},
            {p_patch.edges ? p_patch.edges->area_edge : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->area_edge : nullptr,
                      "area_edge"),
             "area_edge"},
            // --- Untested fields from hardcoded lowering lists ---
            // GRID_METRICS
            {p_patch.edges ? p_patch.edges->inv_dual_edge_length : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->inv_dual_edge_length
                                    : nullptr,
                      "inv_dual_edge_length"),
             "inv_dual_edge_length"},
            {p_patch.edges ? p_patch.edges->inv_primal_edge_length : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->inv_primal_edge_length
                                    : nullptr,
                      "inv_primal_edge_length"),
             "inv_primal_edge_length"},
            {p_patch.cells ? p_patch.cells->area : nullptr,
             safe_vol(p_patch.cells ? p_patch.cells->area : nullptr, "area"),
             "area"},
            {p_metrics.coeff_gradekin,
             safe_vol(p_metrics.coeff_gradekin, "coeff_gradekin"),
             "coeff_gradekin"},
            {p_metrics.wgtfacq_e, safe_vol(p_metrics.wgtfacq_e, "wgtfacq_e"),
             "wgtfacq_e"},
            // INTERPOLATION_COEFFS
            {p_int.geofac_n2s, safe_vol(p_int.geofac_n2s, "geofac_n2s"),
             "geofac_n2s"},
            {p_int.cells_aw_verts,
             safe_vol(p_int.cells_aw_verts, "cells_aw_verts"),
             "cells_aw_verts"},
            {p_patch.edges ? p_patch.edges->tangent_orientation : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->tangent_orientation
                                    : nullptr,
                      "tangent_orientation"),
             "tangent_orientation"},
        };
        std::vector<sensitivity::Field> sens_out = {
            {p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, vn_size,
             "ddt_vn_apc_pc"},
        };
        auto execute_sens = [&](double dt) {
          std::memcpy(p_prog.vn, vn_backup, vn_bytes);
          std::memcpy(p_prog.w, w_backup, w_bytes);
          execute(dt);
          // Sync lp → fp64 so sensitivity::run can read z_ outputs as double.
          for (int i = 0; i < z_kin_n; i++)
            z_kin_hor_e[i] = z_kin_hor_e_lp[i];
          for (int i = 0; i < z_vt_n; i++)
            z_vt_ie[i] = z_vt_ie_lp[i];
          for (int i = 0; i < z_w_n; i++)
            z_w_concorr_me[i] = z_w_concorr_me_lp[i];
        };
        sensitivity::run(execute_sens, dtime, sens_in, sens_out, sens_eps);
        sensitivity::run_combined(execute_sens, dtime, sens_in, sens_out,
                                  sens_safe_names, sens_eps);
      } else {
        for (int r = 0; r < actual_reps; r++) {
          if (actual_reps > 1)
            std::cout << "Timing iteration: " << r << std::endl;
          std::memcpy(p_prog.vn, vn_backup, vn_bytes);
          std::memcpy(p_prog.w, w_backup, w_bytes);

          drain_timing_results();

          Capture cap;
          for (int s = 0; s < substeps; s++) {
            execute(small_dt);
            if (s == 0 && substeps > 1) {
              cudaDeviceSynchronize();
              cap.add(&p_diag.max_vcfl_dyn, sizeof(double), (double)substeps);
              cap.add(z_kin_hor_e_lp.data(), z_kin_n * sizeof(z_kin_hor_e_t));
              cap.add(z_vt_ie_lp.data(), z_vt_n * sizeof(z_vt_ie_t));
              cap.add(z_w_concorr_me_lp.data(),
                      z_w_n * sizeof(z_w_concorr_me_t));
              cap.add(p_diag.vt,
                      meta.at(p_diag.vt).volume() * sizeof(*p_diag.vt));
              cap.add(p_diag.vn_ie,
                      meta.at(p_diag.vn_ie).volume() * sizeof(*p_diag.vn_ie));
              cap.add(p_diag.w_concorr_c, meta.at(p_diag.w_concorr_c).volume() *
                                              sizeof(*p_diag.w_concorr_c));
            }
            if (substeps > 1) {
              int threads = 256;
              integrate_state<<<(vn_size + threads - 1) / threads, threads>>>(
                  p_prog.vn, p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size,
                  small_dt, vn_size);
              if (!lvn_only) {
                dim3 block(32, 8, 1);
                dim3 grid((nproma_val + block.x - 1) / block.x,
                          (nlev_val + block.y - 1) / block.y,
                          (nblks_val + block.z - 1) / block.z);
                integrate_w<<<grid, block>>>(
                    p_prog.w,
                    p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
                    small_dt, nproma_val, nlev_val, nblks_val);
              }
            }
            cudaDeviceSynchronize();
          }
          cap.restore();

          if (substeps > 1) {

            int threads = 256;
            compute_eff_tendency<<<(vn_size + threads - 1) / threads,
                                   threads>>>(
                p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, p_prog.vn,
                vn_backup, dtime, vn_size);
            if (!lvn_only) {
              dim3 block(32, 8, 1);
              dim3 grid((nproma_val + block.x - 1) / block.x,
                        (nlev_val + block.y - 1) / block.y,
                        (nblks_val + block.z - 1) / block.z);
              compute_eff_tendency_w<<<grid, block>>>(
                  p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
                  p_prog.w, w_backup, dtime, nproma_val, nlev_val, nblks_val);
            }
            cudaDeviceSynchronize();
          }
          auto results = drain_timing_results();
          for (const auto &tr : results)
            save_timing_to_db(cfg, r, tr.tag, tr.time_us);
        }

        std::memcpy(p_prog.vn, vn_backup, vn_bytes);
        std::memcpy(p_prog.w, w_backup, w_bytes);
      }

      delete[] vn_backup;
      delete[] w_backup;

      __dace_exit_velocity_no_nproma_if_prop_lvn_only_1_istep_2(h);

    } else if (lvn_only == 0 && istep == 2) {
      auto *h = __dace_init_velocity_no_nproma_if_prop_lvn_only_0_istep_2(
          &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
          z_kin_hor_e_lp.data(), z_vt_ie_lp.data(), z_w_concorr_me_lp.data(),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(0),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(1),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(2),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(0),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(1),
          serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(2),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(0),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(1),
          serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(2),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(2),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(0),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(1),
          serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(2), dt_linintp_ubc,
          dtime, istep, ldeepatmo, lvn_only, ntnd);

      size_t free_bytes_now, total_bytes_now;
      cudaMemGetInfo(&free_bytes_now, &total_bytes_now);
      acout() << "Internal GPU Bytes Allocated: " << get_gpu_mem_usage() << " ("
              << (double)get_gpu_mem_usage() / 1024 / 1024 << " MB)"
              << std::endl;
      acout() << "Real GPU Memory after init: "
              << (double)(total_bytes_now - free_bytes_now) / 1024 / 1024
              << " MB used" << std::endl;
      acout() << "Residency Increase: "
              << (double)(free_bytes_start - free_bytes_now) / 1024 / 1024
              << " MB" << std::endl;
      save_gpu_mem_to_db(cfg, total_bytes_h2d, get_gpu_mem_usage(),
                         total_bytes_now - free_bytes_now,
                         free_bytes_start - free_bytes_now);

      auto execute = [&](double dt) {
        __program_velocity_no_nproma_if_prop_lvn_only_0_istep_2(
            h, &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
            z_kin_hor_e_lp.data(), z_vt_ie_lp.data(), z_w_concorr_me_lp.data(),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(0),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(1),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).size.at(2),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(0),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(1),
            serde::ARRAY_META_DICT()->at(z_vt_ie).size.at(2),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(0),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(1),
            serde::ARRAY_META_DICT()->at(z_w_concorr_me).size.at(2),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_kin_hor_e).lbound.at(2),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(0),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(1),
            serde::ARRAY_META_DICT()->at(z_vt_ie).lbound.at(2), dt_linintp_ubc,
            dt, istep, ldeepatmo, lvn_only, ntnd);
      };

      // Determine array sizes for integration and backup
      auto &meta = *serde::ARRAY_META_DICT();
      auto get_total_size = [](const serde::array_meta &m) {
        int total = 1;
        for (int s : m.size)
          total *= s;
        return total;
      };

      int vn_size = get_total_size(meta.at(p_prog.vn));
      int w_full_size = get_total_size(meta.at(p_prog.w));
      int nproma_val = meta.at(p_prog.w).size.at(0);
      int nlevp1_val = meta.at(p_prog.w).size.at(1);
      int nlev_val = nlevp1_val - 1;
      int nblks_val = meta.at(p_prog.w).size.at(2);
      int w_tend_slice_size = nproma_val * nlev_val * nblks_val;

      double small_dt = dtime / substeps;
      int actual_reps = (substeps > 1) ? 1 : reps;

      // Check DaCe-managed transients if they are accessible
      // (Note: This depends on if they are in the __state or passed as
      // arguments)

      using VnElem = std::remove_pointer_t<decltype(p_prog.vn)>;
      using WElem = std::remove_pointer_t<decltype(p_prog.w)>;
      const size_t vn_bytes = vn_size * sizeof(VnElem);
      const size_t w_bytes = w_full_size * sizeof(WElem);
      VnElem *vn_backup = new VnElem[vn_size];
      WElem *w_backup = new WElem[w_full_size];
      std::memcpy(vn_backup, p_prog.vn, vn_bytes);
      std::memcpy(w_backup, p_prog.w, w_bytes);

      if (sensitivity_mode) {
        std::vector<sensitivity::Field> sens_in = {
            // FP64 prognostics — not reduction candidates
            // {p_prog.vn, vn_size, "vn"},
            // {p_prog.w, w_full_size, "w"},
            // {p_prog.exner, meta.at(p_prog.exner).volume(), "exner"},
            // {p_prog.rho, meta.at(p_prog.rho).volume(), "rho"},
            // {p_prog.theta_v, meta.at(p_prog.theta_v).volume(), "theta_v"},
            // Intermediates from istep==1: hardcoded double — not candidates
            // {z_kin_hor_e, meta.at(z_kin_hor_e).volume(), "z_kin_hor_e"},
            // {z_vt_ie, meta.at(z_vt_ie).volume(), "z_vt_ie"},
            // {p_diag.vt, meta.at(p_diag.vt).volume(), "vt"},
            // {p_diag.vn_ie, meta.at(p_diag.vn_ie).volume(), "vn_ie"},
            // w_concorr_c: LOW_PREC_TYPE input from istep==1 — candidate
            {p_diag.w_concorr_c, safe_vol(p_diag.w_concorr_c, "w_concorr_c"),
             "w_concorr_c"},
            // Interpolation coefficients (LOW_PREC_TYPE)
            {p_int.c_lin_e, safe_vol(p_int.c_lin_e, "c_lin_e"), "c_lin_e"},
            {p_int.e_bln_c_s, safe_vol(p_int.e_bln_c_s, "e_bln_c_s"),
             "e_bln_c_s"},
            {p_int.geofac_grdiv, safe_vol(p_int.geofac_grdiv, "geofac_grdiv"),
             "geofac_grdiv"},
            {p_int.geofac_rot, safe_vol(p_int.geofac_rot, "geofac_rot"),
             "geofac_rot"},
            {p_int.rbf_vec_coeff_e,
             safe_vol(p_int.rbf_vec_coeff_e, "rbf_vec_coeff_e"),
             "rbf_vec_coeff_e"},
            // Metrics (LOW_PREC_TYPE; ddqz_z_full_e hardcoded double — skipped)
            {p_metrics.ddqz_z_half,
             safe_vol(p_metrics.ddqz_z_half, "ddqz_z_half"), "ddqz_z_half"},
            {p_metrics.wgtfac_e, safe_vol(p_metrics.wgtfac_e, "wgtfac_e"),
             "wgtfac_e"},
            {p_metrics.wgtfac_c, safe_vol(p_metrics.wgtfac_c, "wgtfac_c"),
             "wgtfac_c"},
            {p_metrics.coeff1_dwdz,
             safe_vol(p_metrics.coeff1_dwdz, "coeff1_dwdz"), "coeff1_dwdz"},
            {p_metrics.coeff2_dwdz,
             safe_vol(p_metrics.coeff2_dwdz, "coeff2_dwdz"), "coeff2_dwdz"},
            {p_metrics.ddxn_z_full,
             safe_vol(p_metrics.ddxn_z_full, "ddxn_z_full"), "ddxn_z_full"},
            {p_metrics.ddxt_z_full,
             safe_vol(p_metrics.ddxt_z_full, "ddxt_z_full"), "ddxt_z_full"},
            // inv_ddqz_z_full: not used in any SDFG variant — skip
            // {p_metrics.inv_ddqz_z_full, ..., "inv_ddqz_z_full"},
            // Edges (LOW_PREC_TYPE)
            {p_patch.edges ? p_patch.edges->f_e : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->f_e : nullptr, "f_e"),
             "f_e"},
            {p_patch.edges ? p_patch.edges->area_edge : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->area_edge : nullptr,
                      "area_edge"),
             "area_edge"},
            // --- Untested fields from hardcoded lowering lists ---
            // GRID_METRICS
            {p_patch.edges ? p_patch.edges->inv_dual_edge_length : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->inv_dual_edge_length
                                    : nullptr,
                      "inv_dual_edge_length"),
             "inv_dual_edge_length"},
            {p_patch.edges ? p_patch.edges->inv_primal_edge_length : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->inv_primal_edge_length
                                    : nullptr,
                      "inv_primal_edge_length"),
             "inv_primal_edge_length"},
            {p_patch.cells ? p_patch.cells->area : nullptr,
             safe_vol(p_patch.cells ? p_patch.cells->area : nullptr, "area"),
             "area"},
            {p_metrics.coeff_gradekin,
             safe_vol(p_metrics.coeff_gradekin, "coeff_gradekin"),
             "coeff_gradekin"},
            {p_metrics.wgtfacq_e, safe_vol(p_metrics.wgtfacq_e, "wgtfacq_e"),
             "wgtfacq_e"},
            // INTERPOLATION_COEFFS
            {p_int.geofac_n2s, safe_vol(p_int.geofac_n2s, "geofac_n2s"),
             "geofac_n2s"},
            {p_int.cells_aw_verts,
             safe_vol(p_int.cells_aw_verts, "cells_aw_verts"),
             "cells_aw_verts"},
            {p_patch.edges ? p_patch.edges->tangent_orientation : nullptr,
             safe_vol(p_patch.edges ? p_patch.edges->tangent_orientation
                                    : nullptr,
                      "tangent_orientation"),
             "tangent_orientation"},
        };
        std::vector<sensitivity::Field> sens_out = {
            {p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, vn_size,
             "ddt_vn_apc_pc"},
            {p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
             w_tend_slice_size, "ddt_w_adv_pc"},
        };
        auto execute_sens = [&](double dt) {
          std::memcpy(p_prog.vn, vn_backup, vn_bytes);
          std::memcpy(p_prog.w, w_backup, w_bytes);
          execute(dt);
          // Sync lp → fp64 so sensitivity::run can read z_ outputs as double.
          for (int i = 0; i < z_kin_n; i++)
            z_kin_hor_e[i] = z_kin_hor_e_lp[i];
          for (int i = 0; i < z_vt_n; i++)
            z_vt_ie[i] = z_vt_ie_lp[i];
          for (int i = 0; i < z_w_n; i++)
            z_w_concorr_me[i] = z_w_concorr_me_lp[i];
        };
        sensitivity::run(execute_sens, dtime, sens_in, sens_out, sens_eps);
        sensitivity::run_combined(execute_sens, dtime, sens_in, sens_out,
                                  sens_safe_names, sens_eps);
      } else {
        for (int r = 0; r < actual_reps; r++) {
          if (actual_reps > 1)
            std::cout << "Timing iteration: " << r << std::endl;
          std::memcpy(p_prog.vn, vn_backup, vn_bytes);
          std::memcpy(p_prog.w, w_backup, w_bytes);

          drain_timing_results();

          Capture cap;
          for (int s = 0; s < substeps; s++) {
            execute(small_dt);
            if (s == 0 && substeps > 1) {
              cudaDeviceSynchronize();
              cap.add(&p_diag.max_vcfl_dyn, sizeof(double), (double)substeps);
              cap.add(z_kin_hor_e_lp.data(), z_kin_n * sizeof(z_kin_hor_e_t));
              cap.add(z_vt_ie_lp.data(), z_vt_n * sizeof(z_vt_ie_t));
              cap.add(z_w_concorr_me_lp.data(),
                      z_w_n * sizeof(z_w_concorr_me_t));
              cap.add(p_diag.vt,
                      meta.at(p_diag.vt).volume() * sizeof(*p_diag.vt));
              cap.add(p_diag.vn_ie,
                      meta.at(p_diag.vn_ie).volume() * sizeof(*p_diag.vn_ie));
              cap.add(p_diag.w_concorr_c, meta.at(p_diag.w_concorr_c).volume() *
                                              sizeof(*p_diag.w_concorr_c));
            }
            if (substeps > 1) {
              int threads = 256;
              integrate_state<<<(vn_size + threads - 1) / threads, threads>>>(
                  p_prog.vn, p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size,
                  small_dt, vn_size);
              if (!lvn_only) {
                dim3 block(32, 8, 1);
                dim3 grid((nproma_val + block.x - 1) / block.x,
                          (nlev_val + block.y - 1) / block.y,
                          (nblks_val + block.z - 1) / block.z);
                integrate_w<<<grid, block>>>(
                    p_prog.w,
                    p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
                    small_dt, nproma_val, nlev_val, nblks_val);
              }
            }
            cudaDeviceSynchronize();
          }
          cap.restore();

          if (substeps > 1) {

            int threads = 256;
            compute_eff_tendency<<<(vn_size + threads - 1) / threads,
                                   threads>>>(
                p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, p_prog.vn,
                vn_backup, dtime, vn_size);
            if (!lvn_only) {
              dim3 block(32, 8, 1);
              dim3 grid((nproma_val + block.x - 1) / block.x,
                        (nlev_val + block.y - 1) / block.y,
                        (nblks_val + block.z - 1) / block.z);
              compute_eff_tendency_w<<<grid, block>>>(
                  p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size,
                  p_prog.w, w_backup, dtime, nproma_val, nlev_val, nblks_val);
            }
            cudaDeviceSynchronize();
          }
          auto results = drain_timing_results();
          for (const auto &tr : results)
            save_timing_to_db(cfg, r, tr.tag, tr.time_us);
        }

        std::memcpy(p_prog.vn, vn_backup, vn_bytes);
        std::memcpy(p_prog.w, w_backup, w_bytes);
      }

      delete[] vn_backup;
      delete[] w_backup;

      __dace_exit_velocity_no_nproma_if_prop_lvn_only_0_istep_2(h);

    } else {

      throw std::runtime_error("Law of Logic and Mathematics violated");
    }
    acout() << "Step " << n << " done." << std::endl;

    if (!sensitivity_mode && !skip_gotwant) {
      // Sync lp → fp64 so got_want_pair serializes double values.
      for (int i = 0; i < z_kin_n; i++)
        z_kin_hor_e[i] = z_kin_hor_e_lp[i];
      for (int i = 0; i < z_vt_n; i++)
        z_vt_ie[i] = z_vt_ie_lp[i];
      for (int i = 0; i < z_w_n; i++)
        z_w_concorr_me[i] = z_w_concorr_me_lp[i];
      got_want_pair<global_data_type>(global_data, *global_data_want_storage,
                                      "global_data", n, DUMP, cfg);
      got_want_pair<t_nh_diag>(p_diag, *p_diag_want_storage, "p_diag", n, DUMP, cfg);
      got_want_pair<t_nh_metrics>(p_metrics, *p_metrics_want_storage, "p_metrics", n,
                                  DUMP, cfg);
      got_want_pair<t_nh_prog>(p_prog, *p_prog_want_storage, "p_prog", n, DUMP, cfg);
      got_want_pair<double *>(z_kin_hor_e, z_kin_hor_e_want, "z_kin_hor_e", n,
                              DUMP, cfg);
      got_want_pair<double *>(z_vt_ie, z_vt_ie_want, "z_vt_ie", n, DUMP, cfg);
      got_want_pair<double *>(z_w_concorr_me, z_w_concorr_me_want,
                              "z_w_concorr_me", n, DUMP, cfg);
    }
  }
  return EXIT_SUCCESS;
}
