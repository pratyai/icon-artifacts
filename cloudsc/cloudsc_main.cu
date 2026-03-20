#include <iostream>
#include <vector>
#include <chrono>
#include <string>
#include <cstring>
#include <filesystem>
#include <cuda_runtime.h>
#include "h5_utils.h"
#include "codegen/cloudsc_py.h"
#include "cloudsc_constants.h"
#include "cloudsc_inputs.h"

int main(int argc, char** argv) {
    int num_steps = 1;
    bool save_output = true;
    for(int i=1; i<argc; ++i) {
        if(std::string(argv[i]) == "--save") save_output = true;
        else if(i == 1) num_steps = std::stoi(argv[1]);
    }

    int klon = 100, klev = 137, nclv = 5;
    int kidia = 1, kfdia = 100;
    double ptsphy = 3600;

    // Load input data
    std::string input_file = "input.h5";
    hid_t file_id = -1;
    if (std::filesystem::exists(input_file)) {
        std::cout << "Reading input data from: " << input_file << std::endl;
        file_id = H5Fopen(input_file.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
    } else {
        std::cerr << "Warning: " << input_file << " not found, using dummy data." << std::endl;
    }

    std::cout << "Running CloudSC GPU for " << num_steps << " steps." << std::endl;

    CloudSCData data = load_inputs(file_id, klon, klev, nclv);
    if (file_id >= 0) H5Fclose(file_id);

    auto& [ktype, ldcum, pa, pap, paph, pccn, pclv, pcovptot, pdyna, pdyni, pdynl, pfcqlng, pfcqnng, pfcqrng, pfcqsng, pfhpsl, pfhpsn, pfplsl, pfplsn, pfsqif, pfsqitur, pfsqlf, pfsqltur, pfsqrf, pfsqsf, phrlw, phrsw, picrit_aer, plcrit_aer, plsm, plu, plude, pmfd, pmfu, pnice, pq, prainfrac_toprfz, pre_ice, psnde, psupsat, pt, pvervel, pvfa, pvfi, pvfl, tendency_loc_a, tendency_loc_cld, tendency_loc_q, tendency_loc_t, tendency_tmp_a, tendency_tmp_cld, tendency_tmp_q, tendency_tmp_t] = data;

    std::cout << "Initializing DaCe CloudSC GPU program (klon=" << klon << ", klev=" << klev << ")..." << std::endl;
    auto *handle = __dace_init_cloudsc_py(klev, klon, nclv);

    std::cout << "Executing CloudSC GPU..." << std::endl;
    double total_time = 0;
    for (int s=0; s<num_steps; ++s) {
        auto start = std::chrono::high_resolution_clock::now();
        __program_cloudsc_py(handle, ktype, ldcum, pa, pap, paph, pccn, pclv, pcovptot, pdyna, pdyni, pdynl, pfcqlng, pfcqnng, pfcqrng, pfcqsng, pfhpsl, pfhpsn, pfplsl, pfplsn, pfsqif, pfsqitur, pfsqlf, pfsqltur, pfsqrf, pfsqsf, phrlw, phrsw, picrit_aer, plcrit_aer, plsm, plu, plude, pmfd, pmfu, pnice, pq, prainfrac_toprfz, pre_ice, psnde, psupsat, pt, pvervel, pvfa, pvfi, pvfl, tendency_loc_a, tendency_loc_cld, tendency_loc_q, tendency_loc_t, tendency_tmp_a, tendency_tmp_cld, tendency_tmp_q, tendency_tmp_t, kfdia, kidia, klev, klon, nclv, ptsphy, ydcst_rcpd, ydcst_rd, ydcst_retv, ydcst_rg, ydcst_rlmlt, ydcst_rlstt, ydcst_rlvtt, ydcst_rtt, ydcst_rv, ydthf_r2es, ydthf_r3ies, ydthf_r3les, ydthf_r4ies, ydthf_r4les, ydthf_r5alscp, ydthf_r5alvcp, ydthf_r5ies, ydthf_r5les, ydthf_ralfdcp, ydthf_ralsdcp, ydthf_ralvdcp, ydthf_rkoop1, ydthf_rkoop2, ydthf_rtice, ydthf_rticecu, ydthf_rtwat, ydthf_rtwat_rtice_r, ydthf_rtwat_rticecu_r, yrecldp_laericeauto, yrecldp_laericesed, yrecldp_laerliqautolsp, yrecldp_laerliqcoll, yrecldp_ncldtop, yrecldp_nssopt, yrecldp_ramid, yrecldp_ramin, yrecldp_rccn, yrecldp_rcl_apb1, yrecldp_rcl_apb2, yrecldp_rcl_apb3, yrecldp_rcl_cdenom1, yrecldp_rcl_cdenom2, yrecldp_rcl_cdenom3, yrecldp_rcl_const1i, yrecldp_rcl_const1r, yrecldp_rcl_const1s, yrecldp_rcl_const2i, yrecldp_rcl_const2r, yrecldp_rcl_const2s, yrecldp_rcl_const3i, yrecldp_rcl_const3r, yrecldp_rcl_const3s, yrecldp_rcl_const4i, yrecldp_rcl_const4r, yrecldp_rcl_const4s, yrecldp_rcl_const5i, yrecldp_rcl_const5r, yrecldp_rcl_const5s, yrecldp_rcl_const6i, yrecldp_rcl_const6r, yrecldp_rcl_const6s, yrecldp_rcl_const7s, yrecldp_rcl_const8s, yrecldp_rcl_fac1, yrecldp_rcl_fac2, yrecldp_rcl_fzrab, yrecldp_rcl_ka273, yrecldp_rcl_kk_cloud_num_land, yrecldp_rcl_kk_cloud_num_sea, yrecldp_rcl_kkaac, yrecldp_rcl_kkaau, yrecldp_rcl_kkbac, yrecldp_rcl_kkbaun, yrecldp_rcl_kkbauq, yrecldp_rclcrit_land, yrecldp_rclcrit_sea, yrecldp_rcldiff, yrecldp_rcldiff_convi, yrecldp_rcldtopcf, yrecldp_rcovpmin, yrecldp_rdensref, yrecldp_rdepliqrefdepth, yrecldp_rdepliqrefrate, yrecldp_riceinit, yrecldp_rkconv, yrecldp_rkooptau, yrecldp_rlcritsnow, yrecldp_rlmin, yrecldp_rnice, yrecldp_rpecons, yrecldp_rprc1, yrecldp_rprecrhmax, yrecldp_rsnowlin1, yrecldp_rsnowlin2, yrecldp_rtaumel, yrecldp_rthomo, yrecldp_rvice, yrecldp_rvrain, yrecldp_rvrfactor, yrecldp_rvsnow);
        cudaDeviceSynchronize();
        auto end = std::chrono::high_resolution_clock::now();
        total_time += std::chrono::duration<double>(end - start).count();

        if (save_output) {
            std::filesystem::create_directories("outputs_gpu");
            std::string out_name = "outputs_gpu/gpu_output_step_" + std::to_string(s) + ".h5";
            std::filesystem::path full_path = std::filesystem::absolute(out_name);
            hid_t out_id = H5Fcreate(out_name.c_str(), H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);
            save_outputs(out_id, data, klon, klev, nclv);
            H5Fclose(out_id);
            std::cout << "Saved results to: " << full_path << std::endl;
        }
    }

    std::cout << "Total execution time: " << total_time << "s" << std::endl;
    std::cout << "Average time per step: " << total_time / num_steps << "s" << std::endl;

    __dace_exit_cloudsc_py(handle);
    free_inputs(data);

    std::cout << "Done." << std::endl;
    return 0;
}
