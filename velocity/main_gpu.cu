#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <string>
#include <thread>

#include "serde_velocity_no_nproma_gpu.h"
#include "velocity_tendencies_no_nproma_gpu.h"

__global__ void integrate_state(double *field, const double *tendency,
                                double dt, int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n)
    field[i] += tendency[i] * dt;
}

__global__ void integrate_w(double *w, const double *tendency, double dt,
                            int nproma, int nlev, int nblks) {
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

__global__ void compute_eff_tendency(double *tendency,
                                     const double *field_final,
                                     const double *field_orig, double dtime,
                                     int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n)
    tendency[i] = (field_final[i] - field_orig[i]) / dtime;
}

__global__ void compute_eff_tendency_w(double *tendency, const double *w_final,
                                       const double *w_orig, double dtime,
                                       int nproma, int nlev, int nblks) {
  int ic = blockIdx.x * blockDim.x + threadIdx.x;
  int jk = blockIdx.y * blockDim.y + threadIdx.y;
  int jb = blockIdx.z * blockDim.z + threadIdx.z;

  if (ic < nproma && jk < nlev && jb < nblks) {
    if (jk == 0)
      return;
    int w_idx = ic + jk * nproma + jb * nproma * (nlev + 1);
    int t_idx = ic + jk * nproma + jb * nproma * nlev;
    tendency[t_idx] = (w_final[w_idx] - w_orig[w_idx]) / dtime;
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
    CS << s.str();
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
  std::vector<std::jthread> pool;
  auto ft0 = spawn(pool, [&] { return read<T>(ROOT, name + ".t0", timestep); });
  auto ft1 = spawn(pool, [&] { return read<T>(ROOT, name + ".t1", timestep); });
  return {ft0.get(), ft1.get()};
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
              const std::filesystem::path &ROOT) {
  std::jthread tgot([&] {
    open_ofstream(ROOT, name, timestep, "got")
        << serde::serialize_array(got) << std::endl;
  });
  std::jthread twant([&, s = swant] {
    open_ofstream(ROOT, name, timestep, "want") << s << std::endl;
  });

  if (!cfg.db_file.empty()) {
    save_field_to_db(cfg, name, sgot.data(), sgot.size(), swant.data(),
                     swant.size());
  }
}

template <typename T>
std::enable_if_t<std::is_class_v<T> || std::is_arithmetic_v<T>, void>
got_want_pair(const T &got, const T &want, const std::string &name,
              int timestep, const std::filesystem::path &ROOT) {
  std::jthread tgot([&] {
    open_ofstream(ROOT, name, timestep, "got")
        << serde::serialize(&got) << std::endl;
  });
  std::jthread twant([&] {
    open_ofstream(ROOT, name, timestep, "want")
        << serde::serialize(&want) << std::endl;
  });
}

template <>
void got_want_pair(const global_data_type &got, const global_data_type &want,
                   const std::string &name, int timestep,
                   const std::filesystem::path &ROOT) {
  std::jthread tgot([&] {
    open_ofstream(ROOT, name, timestep, "got")
        << serde::serialize_global_data(&got) << std::endl;
  });
  std::jthread twant([&] {
    open_ofstream(ROOT, name, timestep, "want")
        << serde::serialize_global_data(&want) << std::endl;
  });
}

#include <cstdlib> // for std::getenv
#include <filesystem>
#include <sstream>
#include <string>

std::string get_root_path() {
  const char *env_val = std::getenv("NPROMA");
  int nproma = 20480; // default value

  if (env_val != nullptr) {
    std::istringstream iss(env_val);
    int val;
    if (iss >> val && val > 0) {
      nproma = val;
    }
  }

  return "data_nproma" + std::to_string(nproma);
}
int get_reps() {
  const char *env_val = std::getenv("REPS");
  int reps = 20; // default value

  if (env_val != nullptr) {
    std::istringstream iss(env_val);
    int val;
    if (iss >> val && val > 0) {
      reps = val;
    }
  }

  return reps;
}
int get_substeps() {
  const char *env_val = std::getenv("SUBSTEPS");
  int substeps = 1; // default value

  if (env_val != nullptr) {
    std::istringstream iss(env_val);
    int val;
    if (iss >> val && val > 0) {
      substeps = val;
    }
  }

  return substeps;
}

int main(int argc, char *argv[]) {
  std::string nproma_path_str = get_root_path();
  const std::filesystem::path ROOT{nproma_path_str};
  acerr() << "Will be reading data from: " << ROOT << std::endl;
  const std::filesystem::path DUMP =
      std::filesystem::current_path() / "gotwant" / ROOT.filename();
  std::error_code ec;
  if (!std::filesystem::create_directories(DUMP, ec) && ec) {
    acerr() << "Failed to create directory: " << ec.message() << std::endl;
  }
  acerr() << "Will be writing got and want files to: " << DUMP << std::endl;

  std::vector<int> ns = {1, 2, 7, 9, 43, 93, 463, 519, 1140, 1814, 2593, 5701};
  int n1 = -1;
  int rep = get_reps();
  int substeps = get_substeps();

  if (argc == 2) {
    n1 = std::atoi(argv[1]);
  }
  acout() << "Running: " << n1 << std::endl;
  if (n1 > 0) {
    ns = {n1};
  }

  for (int n : ns) {
    acerr() << "Reading data for " << n << "..." << std::endl;

    std::vector<std::jthread> pool;

    auto fut_global_data = spawn(pool, [&] {
      return t0_t1_pair<global_data_type>(ROOT, "global_data", n);
    });
    auto fut_p_diag =
        spawn(pool, [&] { return t0_t1_pair<t_nh_diag>(ROOT, "p_diag", n); });
    auto fut_p_int =
        spawn(pool, [&] { return read<t_int_state>(ROOT, "p_int", n); });
    auto fut_p_metrics = spawn(
        pool, [&] { return t0_t1_pair<t_nh_metrics>(ROOT, "p_metrics", n); });
    auto fut_p_patch =
        spawn(pool, [&] { return read<t_patch>(ROOT, "p_patch", n); });
    auto fut_p_prog =
        spawn(pool, [&] { return t0_t1_pair<t_nh_prog>(ROOT, "p_prog", n); });
    auto fut_z_kin_hor_e = spawn(
        pool, [&] { return t0_t1_pair<double *>(ROOT, "z_kin_hor_e", n); });
    auto fut_z_vt_ie =
        spawn(pool, [&] { return t0_t1_pair<double *>(ROOT, "z_vt_ie", n); });
    auto fut_z_w_concorr = spawn(
        pool, [&] { return t0_t1_pair<double *>(ROOT, "z_w_concorr_me", n); });
    auto fut_istep = spawn(pool, [&] { return read<int>(ROOT, "istep", n); });
    auto fut_ldeepatmo =
        spawn(pool, [&] { return read<int>(ROOT, "ldeepatmo", n); });
    auto fut_lvn_only =
        spawn(pool, [&] { return read<int>(ROOT, "lvn_only", n); });
    auto fut_ntnd = spawn(pool, [&] { return read<int>(ROOT, "ntnd", n); });
    auto fut_dt_linintp =
        spawn(pool, [&] { return read<double>(ROOT, "dt_linintp_ubc", n); });
    auto fut_dtime =
        spawn(pool, [&] { return read<double>(ROOT, "dtime", n); });
    pool.clear();

    auto global_data_pair = fut_global_data.get();
    auto &global_data = std::get<0>(global_data_pair);
    auto &global_data_want = std::get<1>(global_data_pair);

    auto p_diag_pair = fut_p_diag.get();
    auto &p_diag = std::get<0>(p_diag_pair);
    auto &p_diag_want = std::get<1>(p_diag_pair);

    auto p_int = fut_p_int.get();

    auto p_metrics_pair = fut_p_metrics.get();
    auto &p_metrics = std::get<0>(p_metrics_pair);
    auto &p_metrics_want = std::get<1>(p_metrics_pair);

    auto p_patch = fut_p_patch.get();

    auto p_prog_pair = fut_p_prog.get();
    auto &p_prog = std::get<0>(p_prog_pair);
    auto &p_prog_want = std::get<1>(p_prog_pair);

    auto z_kin_hor_e_pair = fut_z_kin_hor_e.get();
    auto &z_kin_hor_e = std::get<0>(z_kin_hor_e_pair);
    auto &z_kin_hor_e_want = std::get<1>(z_kin_hor_e_pair);

    auto z_vt_ie_pair = fut_z_vt_ie.get();
    auto &z_vt_ie = std::get<0>(z_vt_ie_pair);
    auto &z_vt_ie_want = std::get<1>(z_vt_ie_pair);

    auto z_w_concorr_me_pair = fut_z_w_concorr.get();
    auto &z_w_concorr_me = std::get<0>(z_w_concorr_me_pair);
    auto &z_w_concorr_me_want = std::get<1>(z_w_concorr_me_pair);
    int istep = fut_istep.get();
    int ldeepatmo = fut_ldeepatmo.get();
    int lvn_only = fut_lvn_only.get();
    int ntnd = fut_ntnd.get();
    double dt_linintp_ubc = fut_dt_linintp.get();
    double dtime = fut_dtime.get();

    RunConfig cfg{precision_name<LOW_PREC_TYPE>(),
                  substeps,
                  n,
                  global_data.nproma,
                  istep,
                  lvn_only,
                  ldeepatmo,
                  db_file};
    acerr() << "All data read..." << std::endl;

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

    if (lvn_only == 1 && istep == 1) {
      auto *h = __dace_init_velocity_no_nproma_if_prop_lvn_only_1_istep_1(
          &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
          z_kin_hor_e, z_vt_ie, z_w_concorr_me,
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

      auto execute = [&](double dt) {
        __program_velocity_no_nproma_if_prop_lvn_only_1_istep_1(
            h, &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
            z_kin_hor_e, z_vt_ie, z_w_concorr_me,
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
      int actual_reps = (substeps > 1) ? 1 : rep;

      double *vn_backup, *w_backup;
      cudaMalloc(&vn_backup, vn_size * sizeof(double));
      cudaMalloc(&w_backup, w_full_size * sizeof(double));
      cudaMemcpy(vn_backup, p_prog.vn, vn_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaMemcpy(w_backup, p_prog.w, w_full_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);

      for (int r = 0; r < actual_reps; r++) {
        if (actual_reps > 1)
          std::cout << "Timing iteration: " << r << std::endl;
        cudaMemcpy(p_prog.vn, vn_backup, vn_size * sizeof(double),
                   cudaMemcpyDeviceToDevice);
        cudaMemcpy(p_prog.w, w_backup, w_full_size * sizeof(double),
                   cudaMemcpyDeviceToDevice);

        for (int s = 0; s < substeps; s++) {
          execute(small_dt);
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
        if (substeps > 1) {
          int threads = 256;
          compute_eff_tendency<<<(vn_size + threads - 1) / threads, threads>>>(
              p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, p_prog.vn, vn_backup,
              dtime, vn_size);
          if (!lvn_only) {
            dim3 block(32, 8, 1);
            dim3 grid((nproma_val + block.x - 1) / block.x,
                      (nlev_val + block.y - 1) / block.y,
                      (nblks_val + block.z - 1) / block.z);
            compute_eff_tendency_w<<<grid, block>>>(
                p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size, p_prog.w,
                w_backup, dtime, nproma_val, nlev_val, nblks_val);
          }
          cudaDeviceSynchronize();
        }
      }
      cudaMemcpy(p_prog.vn, vn_backup, vn_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaMemcpy(p_prog.w, w_backup, w_full_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaFree(vn_backup);
      cudaFree(w_backup);
      __dace_exit_velocity_no_nproma_if_prop_lvn_only_1_istep_1(h);

    } else if (lvn_only == 0 && istep == 1) {
      auto *h = __dace_init_velocity_no_nproma_if_prop_lvn_only_0_istep_1(
          &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
          z_kin_hor_e, z_vt_ie, z_w_concorr_me,
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

      auto execute = [&](double dt) {
        __program_velocity_no_nproma_if_prop_lvn_only_0_istep_1(
            h, &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
            z_kin_hor_e, z_vt_ie, z_w_concorr_me,
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
      int actual_reps = (substeps > 1) ? 1 : rep;

      double *vn_backup, *w_backup;
      cudaMalloc(&vn_backup, vn_size * sizeof(double));
      cudaMalloc(&w_backup, w_full_size * sizeof(double));
      cudaMemcpy(vn_backup, p_prog.vn, vn_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaMemcpy(w_backup, p_prog.w, w_full_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);

      for (int r = 0; r < actual_reps; r++) {
        if (actual_reps > 1)
          std::cout << "Timing iteration: " << r << std::endl;
        cudaMemcpy(p_prog.vn, vn_backup, vn_size * sizeof(double),
                   cudaMemcpyDeviceToDevice);
        cudaMemcpy(p_prog.w, w_backup, w_full_size * sizeof(double),
                   cudaMemcpyDeviceToDevice);

        for (int s = 0; s < substeps; s++) {
          execute(small_dt);
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
        if (substeps > 1) {
          int threads = 256;
          compute_eff_tendency<<<(vn_size + threads - 1) / threads, threads>>>(
              p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, p_prog.vn, vn_backup,
              dtime, vn_size);
          if (!lvn_only) {
            dim3 block(32, 8, 1);
            dim3 grid((nproma_val + block.x - 1) / block.x,
                      (nlev_val + block.y - 1) / block.y,
                      (nblks_val + block.z - 1) / block.z);
            compute_eff_tendency_w<<<grid, block>>>(
                p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size, p_prog.w,
                w_backup, dtime, nproma_val, nlev_val, nblks_val);
          }
          cudaDeviceSynchronize();
        }
      }
      cudaMemcpy(p_prog.vn, vn_backup, vn_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaMemcpy(p_prog.w, w_backup, w_full_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaFree(vn_backup);
      cudaFree(w_backup);
      __dace_exit_velocity_no_nproma_if_prop_lvn_only_0_istep_1(h);

    } else if (lvn_only == 1 && istep == 2) {
      auto *h = __dace_init_velocity_no_nproma_if_prop_lvn_only_1_istep_2(
          &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
          z_kin_hor_e, z_vt_ie, z_w_concorr_me,
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

      auto execute = [&](double dt) {
        __program_velocity_no_nproma_if_prop_lvn_only_1_istep_2(
            h, &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
            z_kin_hor_e, z_vt_ie, z_w_concorr_me,
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
      int actual_reps = (substeps > 1) ? 1 : rep;

      double *vn_backup, *w_backup;
      cudaMalloc(&vn_backup, vn_size * sizeof(double));
      cudaMalloc(&w_backup, w_full_size * sizeof(double));
      cudaMemcpy(vn_backup, p_prog.vn, vn_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaMemcpy(w_backup, p_prog.w, w_full_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);

      for (int r = 0; r < actual_reps; r++) {
        if (actual_reps > 1)
          std::cout << "Timing iteration: " << r << std::endl;
        cudaMemcpy(p_prog.vn, vn_backup, vn_size * sizeof(double),
                   cudaMemcpyDeviceToDevice);
        cudaMemcpy(p_prog.w, w_backup, w_full_size * sizeof(double),
                   cudaMemcpyDeviceToDevice);

        for (int s = 0; s < substeps; s++) {
          execute(small_dt);
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
        if (substeps > 1) {
          int threads = 256;
          compute_eff_tendency<<<(vn_size + threads - 1) / threads, threads>>>(
              p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, p_prog.vn, vn_backup,
              dtime, vn_size);
          if (!lvn_only) {
            dim3 block(32, 8, 1);
            dim3 grid((nproma_val + block.x - 1) / block.x,
                      (nlev_val + block.y - 1) / block.y,
                      (nblks_val + block.z - 1) / block.z);
            compute_eff_tendency_w<<<grid, block>>>(
                p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size, p_prog.w,
                w_backup, dtime, nproma_val, nlev_val, nblks_val);
          }
          cudaDeviceSynchronize();
        }
      }
      cudaMemcpy(p_prog.vn, vn_backup, vn_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaMemcpy(p_prog.w, w_backup, w_full_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaFree(vn_backup);
      cudaFree(w_backup);
      __dace_exit_velocity_no_nproma_if_prop_lvn_only_1_istep_2(h);

    } else if (lvn_only == 0 && istep == 2) {
      auto *h = __dace_init_velocity_no_nproma_if_prop_lvn_only_0_istep_2(
          &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
          z_kin_hor_e, z_vt_ie, z_w_concorr_me,
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

      auto execute = [&](double dt) {
        __program_velocity_no_nproma_if_prop_lvn_only_0_istep_2(
            h, &global_data, &p_diag, &p_int, &p_metrics, &p_patch, &p_prog,
            z_kin_hor_e, z_vt_ie, z_w_concorr_me,
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
      int actual_reps = (substeps > 1) ? 1 : rep;

      double *vn_backup, *w_backup;
      cudaMalloc(&vn_backup, vn_size * sizeof(double));
      cudaMalloc(&w_backup, w_full_size * sizeof(double));
      cudaMemcpy(vn_backup, p_prog.vn, vn_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaMemcpy(w_backup, p_prog.w, w_full_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);

      for (int r = 0; r < actual_reps; r++) {
        if (actual_reps > 1)
          std::cout << "Timing iteration: " << r << std::endl;
        cudaMemcpy(p_prog.vn, vn_backup, vn_size * sizeof(double),
                   cudaMemcpyDeviceToDevice);
        cudaMemcpy(p_prog.w, w_backup, w_full_size * sizeof(double),
                   cudaMemcpyDeviceToDevice);

        for (int s = 0; s < substeps; s++) {
          execute(small_dt);
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
        if (substeps > 1) {
          int threads = 256;
          compute_eff_tendency<<<(vn_size + threads - 1) / threads, threads>>>(
              p_diag.ddt_vn_apc_pc + (ntnd - 1) * vn_size, p_prog.vn, vn_backup,
              dtime, vn_size);
          if (!lvn_only) {
            dim3 block(32, 8, 1);
            dim3 grid((nproma_val + block.x - 1) / block.x,
                      (nlev_val + block.y - 1) / block.y,
                      (nblks_val + block.z - 1) / block.z);
            compute_eff_tendency_w<<<grid, block>>>(
                p_diag.ddt_w_adv_pc + (ntnd - 1) * w_tend_slice_size, p_prog.w,
                w_backup, dtime, nproma_val, nlev_val, nblks_val);
          }
          cudaDeviceSynchronize();
        }
      }
      cudaMemcpy(p_prog.vn, vn_backup, vn_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaMemcpy(p_prog.w, w_backup, w_full_size * sizeof(double),
                 cudaMemcpyDeviceToDevice);
      cudaFree(vn_backup);
      cudaFree(w_backup);
      __dace_exit_velocity_no_nproma_if_prop_lvn_only_0_istep_2(h);

    } else {

      throw std::runtime_error("Law of Logic and Mathematics violated");
    }
    acout() << "Step " << n << " done." << std::endl;

    pool.emplace_back([&] {
      got_want_pair<global_data_type>(global_data, global_data_want,
                                      "global_data", n, DUMP);
    });
    pool.emplace_back([&] {
      got_want_pair<t_nh_diag>(p_diag, p_diag_want, "p_diag", n, DUMP);
    });
    pool.emplace_back([&] {
      got_want_pair<t_nh_metrics>(p_metrics, p_metrics_want, "p_metrics", n,
                                  DUMP);
    });
    pool.emplace_back([&] {
      got_want_pair<t_nh_prog>(p_prog, p_prog_want, "p_prog", n, DUMP);
    });
    pool.emplace_back([&] {
      got_want_pair<double *>(z_kin_hor_e, z_kin_hor_e_want, "z_kin_hor_e", n,
                              DUMP);
    });
    pool.emplace_back([&] {
      got_want_pair<double *>(z_vt_ie, z_vt_ie_want, "z_vt_ie", n, DUMP);
    });
    pool.emplace_back([&] {
      got_want_pair<double *>(z_w_concorr_me, z_w_concorr_me_want,
                              "z_w_concorr_me", n, DUMP);
    });
    pool.clear();
  }
  return EXIT_SUCCESS;
}
