#include <iostream>
#include <vector>
#include <chrono>
#include <string>
#include <cstring>
#include <filesystem>
#include "h5_utils.h"
#include "cloudsc_py.h"

#ifndef CLOUDSC_PREC_TAG
#define CLOUDSC_PREC_TAG "unk"
#endif
#include "sensitivity.h"
#include "cloudsc_constants.h"
#include "cloudsc_inputs.h"

int main(int argc, char** argv) {
    int num_steps = 1;
    int num_reps = 1;
    int klon_tile = 1;
    if (argc > 1 && std::string(argv[1]) != "--save" && std::string(argv[1]) != "--sensitivity"
                 && std::string(argv[1]).rfind("--reps", 0) != 0
                 && std::string(argv[1]).rfind("--klon-tile", 0) != 0) num_steps = std::stoi(argv[1]);
    bool save_output = true;
    bool sensitivity_mode = false;
    double sens_eps = 1e-5;
    for(int i=1; i<argc; ++i) {
        std::string a = argv[i];
        if(a == "--save") save_output = true;
        else if(a == "--sensitivity") sensitivity_mode = true;
        else if(a == "--sens-eps" && i+1 < argc) sens_eps = std::stod(argv[++i]);
        else if(a.rfind("--reps=", 0) == 0) num_reps = std::stoi(a.substr(7));
        else if(a == "--reps" && i+1 < argc) num_reps = std::stoi(argv[++i]);
        else if(a.rfind("--klon-tile=", 0) == 0) klon_tile = std::stoi(a.substr(12));
        else if(a == "--klon-tile" && i+1 < argc) klon_tile = std::stoi(argv[++i]);
    }

    int klon = 100 * klon_tile, klev = 137, nclv = 5;
    int klon_native = 100;
    int kidia = 1, kfdia = 100;
    double ptsphy = 3600;

    std::string input_file = "input.h5";
    hid_t file_id = -1;
    if (std::filesystem::exists(input_file)) {
        std::cout << "Reading input data from: " << input_file << std::endl;
        file_id = H5Fopen(input_file.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
    } else {
        std::cerr << "Warning: " << input_file << " not found, using dummy data." << std::endl;
    }

    std::cout << "Running CloudSC for " << num_steps << " steps." << std::endl;

    CloudSCData data = (klon_tile > 1)
        ? load_inputs_tiled(file_id, klon_native, klev, nclv, klon)
        : load_inputs(file_id, klon, klev, nclv);
    if (file_id >= 0) H5Fclose(file_id);

    auto& [ktype, ldcum, pa, pap, paph, pccn, pclv, pcovptot, pdyna, pdyni, pdynl, pfcqlng, pfcqnng, pfcqrng, pfcqsng, pfhpsl, pfhpsn, pfplsl, pfplsn, pfsqif, pfsqitur, pfsqlf, pfsqltur, pfsqrf, pfsqsf, phrlw, phrsw, picrit_aer, plcrit_aer, plsm, plu, plude, pmfd, pmfu, pnice, pq, prainfrac_toprfz, pre_ice, psnde, psupsat, pt, pvervel, pvfa, pvfi, pvfl, tendency_loc_a, tendency_loc_cld, tendency_loc_q, tendency_loc_t, tendency_tmp_a, tendency_tmp_cld, tendency_tmp_q, tendency_tmp_t] = data;

    std::cout << "Initializing DaCe CloudSC CPU program (klon=" << klon << ", klev=" << klev << ")..." << std::endl;
    auto *handle = __dace_init_cloudsc_py(klev, klon, nclv);

    std::cout << "Executing CloudSC (" << num_reps << " rep(s) x " << num_steps << " step(s))..." << std::endl;
    double total_time = 0;
    for (int r=0; r<num_reps; ++r)
    for (int s=0; s<num_steps; ++s) {
        auto start = std::chrono::high_resolution_clock::now();
        __program_cloudsc_py(handle, ktype, ldcum, pa, pap, paph, pccn, pclv, pcovptot, pdyna, pdyni, pdynl, pfcqlng, pfcqnng, pfcqrng, pfcqsng, pfhpsl, pfhpsn, pfplsl, pfplsn, pfsqif, pfsqitur, pfsqlf, pfsqltur, pfsqrf, pfsqsf, phrlw, phrsw, picrit_aer, plcrit_aer, plsm, plu, plude, pmfd, pmfu, pnice, pq, prainfrac_toprfz, pre_ice, psnde, psupsat, pt, pvervel, pvfa, pvfi, pvfl, tendency_loc_a, tendency_loc_cld, tendency_loc_q, tendency_loc_t, tendency_tmp_a, tendency_tmp_cld, tendency_tmp_q, tendency_tmp_t, kfdia, kidia, klev, klon, nclv, ptsphy, ydcst_rcpd, ydcst_rd, ydcst_retv, ydcst_rg, ydcst_rlmlt, ydcst_rlstt, ydcst_rlvtt, ydcst_rtt, ydcst_rv, ydthf_r2es, ydthf_r3ies, ydthf_r3les, ydthf_r4ies, ydthf_r4les, ydthf_r5alscp, ydthf_r5alvcp, ydthf_r5ies, ydthf_r5les, ydthf_ralfdcp, ydthf_ralsdcp, ydthf_ralvdcp, ydthf_rkoop1, ydthf_rkoop2, ydthf_rtice, ydthf_rticecu, ydthf_rtwat, ydthf_rtwat_rtice_r, ydthf_rtwat_rticecu_r, yrecldp_laericeauto, yrecldp_laericesed, yrecldp_laerliqautolsp, yrecldp_laerliqcoll, yrecldp_ncldtop, yrecldp_nssopt, yrecldp_ramid, yrecldp_ramin, yrecldp_rccn, yrecldp_rcl_apb1, yrecldp_rcl_apb2, yrecldp_rcl_apb3, yrecldp_rcl_cdenom1, yrecldp_rcl_cdenom2, yrecldp_rcl_cdenom3, yrecldp_rcl_const1i, yrecldp_rcl_const1r, yrecldp_rcl_const1s, yrecldp_rcl_const2i, yrecldp_rcl_const2r, yrecldp_rcl_const2s, yrecldp_rcl_const3i, yrecldp_rcl_const3r, yrecldp_rcl_const3s, yrecldp_rcl_const4i, yrecldp_rcl_const4r, yrecldp_rcl_const4s, yrecldp_rcl_const5i, yrecldp_rcl_const5r, yrecldp_rcl_const5s, yrecldp_rcl_const6i, yrecldp_rcl_const6r, yrecldp_rcl_const6s, yrecldp_rcl_const7s, yrecldp_rcl_const8s, yrecldp_rcl_fac1, yrecldp_rcl_fac2, yrecldp_rcl_fzrab, yrecldp_rcl_ka273, yrecldp_rcl_kk_cloud_num_land, yrecldp_rcl_kk_cloud_num_sea, yrecldp_rcl_kkaac, yrecldp_rcl_kkaau, yrecldp_rcl_kkbac, yrecldp_rcl_kkbaun, yrecldp_rcl_kkbauq, yrecldp_rclcrit_land, yrecldp_rclcrit_sea, yrecldp_rcldiff, yrecldp_rcldiff_convi, yrecldp_rcldtopcf, yrecldp_rcovpmin, yrecldp_rdensref, yrecldp_rdepliqrefdepth, yrecldp_rdepliqrefrate, yrecldp_riceinit, yrecldp_rkconv, yrecldp_rkooptau, yrecldp_rlcritsnow, yrecldp_rlmin, yrecldp_rnice, yrecldp_rpecons, yrecldp_rprc1, yrecldp_rprecrhmax, yrecldp_rsnowlin1, yrecldp_rsnowlin2, yrecldp_rtaumel, yrecldp_rthomo, yrecldp_rvice, yrecldp_rvrain, yrecldp_rvrfactor, yrecldp_rvsnow);
        auto end = std::chrono::high_resolution_clock::now();
        total_time += std::chrono::duration<double>(end - start).count();

        if (save_output) {
            std::string out_dir = std::string("build/outputs/cpp/") + CLOUDSC_PREC_TAG;
            std::filesystem::create_directories(out_dir);
            std::string out_name = out_dir + "/cpp_output_step_" + std::to_string(s) + ".h5";
            hid_t out_id = H5Fcreate(out_name.c_str(), H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);
            save_outputs(out_id, data, klon, klev, nclv);
            H5Fclose(out_id);
        }
    }

    std::cout << "Total execution time: " << total_time << "s" << std::endl;

    if (sensitivity_mode) {
        if (std::filesystem::exists(input_file))
            file_id = H5Fopen(input_file.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
        auto reload = [&](double* dst, const char* name, size_t n) {
            if (file_id >= 0 && H5Lexists(file_id, name, H5P_DEFAULT) > 0) {
                hid_t ds = H5Dopen2(file_id, name, H5P_DEFAULT);
                H5Dread(ds, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, dst);
                H5Dclose(ds);
            }
        };
        size_t n2 = klon * klev, n3 = klon * (klev + 1), n4 = klon * klev * nclv;
        auto restore_all = [&]() {
            reload(pt, "PT", n2); reload(pq, "PQ", n2);
            reload(tendency_tmp_t, "TENDENCY_TMP_T", n2); reload(tendency_tmp_q, "TENDENCY_TMP_Q", n2);
            reload(tendency_tmp_a, "TENDENCY_TMP_A", n2); reload(tendency_tmp_cld, "TENDENCY_TMP_CLD", n4);
            reload(tendency_loc_t, "TENDENCY_LOC_T", n2); reload(tendency_loc_q, "TENDENCY_LOC_Q", n2);
            reload(tendency_loc_a, "TENDENCY_LOC_A", n2); reload(tendency_loc_cld, "TENDENCY_LOC_CLD", n4);
            reload(pvfa, "PVFA", n2); reload(pvfl, "PVFL", n2); reload(pvfi, "PVFI", n2);
            reload(pdyna, "PDYNA", n2); reload(pdynl, "PDYNL", n2); reload(pdyni, "PDYNI", n2);
            reload(phrsw, "PHRSW", n2); reload(phrlw, "PHRLW", n2);
            reload(pvervel, "PVERVEL", n2); reload(pap, "PAP", n2); reload(paph, "PAPH", n2);
            reload(plsm, "PLSM", n2); reload(plu, "PLU", n2); reload(plude, "PLUDE", n2);
            reload(psnde, "PSNDE", n2); reload(pmfu, "PMFU", n2); reload(pmfd, "PMFD", n2);
            reload(pa, "PA", n2); reload(pclv, "PCLV", n2); reload(psupsat, "PSUPSAT", n2);
            reload(plcrit_aer, "PLCRIT_AER", n2); reload(picrit_aer, "PICRIT_AER", n2);
            reload(pre_ice, "PRE_ICE", n2); reload(pccn, "PCCN", n2); reload(pnice, "PNICE", n2);
            reload(pcovptot, "PCOVPTOT", n2); reload(prainfrac_toprfz, "PRAINFRAC_TOPRFZ", n2);
        };
        auto execute = [&]() {
            __program_cloudsc_py(handle, ktype, ldcum, pa, pap, paph, pccn, pclv, pcovptot, pdyna, pdyni, pdynl, pfcqlng, pfcqnng, pfcqrng, pfcqsng, pfhpsl, pfhpsn, pfplsl, pfplsn, pfsqif, pfsqitur, pfsqlf, pfsqltur, pfsqrf, pfsqsf, phrlw, phrsw, picrit_aer, plcrit_aer, plsm, plu, plude, pmfd, pmfu, pnice, pq, prainfrac_toprfz, pre_ice, psnde, psupsat, pt, pvervel, pvfa, pvfi, pvfl, tendency_loc_a, tendency_loc_cld, tendency_loc_q, tendency_loc_t, tendency_tmp_a, tendency_tmp_cld, tendency_tmp_q, tendency_tmp_t, kfdia, kidia, klev, klon, nclv, ptsphy, ydcst_rcpd, ydcst_rd, ydcst_retv, ydcst_rg, ydcst_rlmlt, ydcst_rlstt, ydcst_rlvtt, ydcst_rtt, ydcst_rv, ydthf_r2es, ydthf_r3ies, ydthf_r3les, ydthf_r4ies, ydthf_r4les, ydthf_r5alscp, ydthf_r5alvcp, ydthf_r5ies, ydthf_r5les, ydthf_ralfdcp, ydthf_ralsdcp, ydthf_ralvdcp, ydthf_rkoop1, ydthf_rkoop2, ydthf_rtice, ydthf_rticecu, ydthf_rtwat, ydthf_rtwat_rtice_r, ydthf_rtwat_rticecu_r, yrecldp_laericeauto, yrecldp_laericesed, yrecldp_laerliqautolsp, yrecldp_laerliqcoll, yrecldp_ncldtop, yrecldp_nssopt, yrecldp_ramid, yrecldp_ramin, yrecldp_rccn, yrecldp_rcl_apb1, yrecldp_rcl_apb2, yrecldp_rcl_apb3, yrecldp_rcl_cdenom1, yrecldp_rcl_cdenom2, yrecldp_rcl_cdenom3, yrecldp_rcl_const1i, yrecldp_rcl_const1r, yrecldp_rcl_const1s, yrecldp_rcl_const2i, yrecldp_rcl_const2r, yrecldp_rcl_const2s, yrecldp_rcl_const3i, yrecldp_rcl_const3r, yrecldp_rcl_const3s, yrecldp_rcl_const4i, yrecldp_rcl_const4r, yrecldp_rcl_const4s, yrecldp_rcl_const5i, yrecldp_rcl_const5r, yrecldp_rcl_const5s, yrecldp_rcl_const6i, yrecldp_rcl_const6r, yrecldp_rcl_const6s, yrecldp_rcl_const7s, yrecldp_rcl_const8s, yrecldp_rcl_fac1, yrecldp_rcl_fac2, yrecldp_rcl_fzrab, yrecldp_rcl_ka273, yrecldp_rcl_kk_cloud_num_land, yrecldp_rcl_kk_cloud_num_sea, yrecldp_rcl_kkaac, yrecldp_rcl_kkaau, yrecldp_rcl_kkbac, yrecldp_rcl_kkbaun, yrecldp_rcl_kkbauq, yrecldp_rclcrit_land, yrecldp_rclcrit_sea, yrecldp_rcldiff, yrecldp_rcldiff_convi, yrecldp_rcldtopcf, yrecldp_rcovpmin, yrecldp_rdensref, yrecldp_rdepliqrefdepth, yrecldp_rdepliqrefrate, yrecldp_riceinit, yrecldp_rkconv, yrecldp_rkooptau, yrecldp_rlcritsnow, yrecldp_rlmin, yrecldp_rnice, yrecldp_rpecons, yrecldp_rprc1, yrecldp_rprecrhmax, yrecldp_rsnowlin1, yrecldp_rsnowlin2, yrecldp_rtaumel, yrecldp_rthomo, yrecldp_rvice, yrecldp_rvrain, yrecldp_rvrfactor, yrecldp_rvsnow);
        };
        std::vector<sensitivity::Field> sens_inputs = {
            {pt, (int)n2, "pt"}, {pq, (int)n2, "pq"},
            {tendency_tmp_t, (int)n2, "tendency_tmp_t"}, {tendency_tmp_q, (int)n2, "tendency_tmp_q"},
            {tendency_tmp_a, (int)n2, "tendency_tmp_a"}, {tendency_tmp_cld, (int)n4, "tendency_tmp_cld"},
            {pvfa, (int)n2, "pvfa"}, {pvfl, (int)n2, "pvfl"}, {pvfi, (int)n2, "pvfi"},
            {pdyna, (int)n2, "pdyna"}, {pdynl, (int)n2, "pdynl"}, {pdyni, (int)n2, "pdyni"},
            {phrsw, (int)n2, "phrsw"}, {phrlw, (int)n2, "phrlw"},
            {pvervel, (int)n2, "pvervel"}, {pap, (int)n2, "pap"}, {paph, (int)n2, "paph"},
            {plsm, (int)klon, "plsm"}, {plu, (int)n2, "plu"}, {plude, (int)n2, "plude"},
            {psnde, (int)n2, "psnde"}, {pmfu, (int)n2, "pmfu"}, {pmfd, (int)n2, "pmfd"},
            {pa, (int)n2, "pa"}, {pclv, (int)n4, "pclv"}, {psupsat, (int)n2, "psupsat"},
            {plcrit_aer, (int)n2, "plcrit_aer"}, {picrit_aer, (int)n2, "picrit_aer"},
            {pre_ice, (int)n2, "pre_ice"}, {pccn, (int)n2, "pccn"}, {pnice, (int)n2, "pnice"},
        };
        std::vector<sensitivity::Field> sens_outputs = {
            {tendency_loc_t, (int)n2, "tendency_loc_t"}, {tendency_loc_q, (int)n2, "tendency_loc_q"},
            {tendency_loc_a, (int)n2, "tendency_loc_a"}, {tendency_loc_cld, (int)n4, "tendency_loc_cld"},
            {pcovptot, (int)n2, "pcovptot"}, {pfsqlf, (int)n3, "pfsqlf"},
            {pfsqrf, (int)n3, "pfsqrf"}, {pfsqif, (int)n3, "pfsqif"},
            {pfsqsf, (int)n3, "pfsqsf"}, {pfcqlng, (int)n3, "pfcqlng"},
            {pfcqnng, (int)n3, "pfcqnng"}, {pfcqrng, (int)n3, "pfcqrng"},
            {pfcqsng, (int)n3, "pfcqsng"}, {pfhpsn, (int)n3, "pfhpsn"},
            {pfplsn, (int)n3, "pfplsn"}, {pfsqltur, (int)n3, "pfsqltur"},
            {pfsqitur, (int)n3, "pfsqitur"}, {pfhpsl, (int)n3, "pfhpsl"},
            {pfplsl, (int)n3, "pfplsl"}, {prainfrac_toprfz, (int)klon, "prainfrac_toprfz"},
        };
        std::cerr << "Running sensitivity analysis (eps=" << sens_eps << ")...\n";
        sensitivity::run(restore_all, execute, sens_inputs, sens_outputs, sens_eps);
        if (file_id >= 0) H5Fclose(file_id);
    }

    __dace_exit_cloudsc_py(handle);
    free_inputs(data);
    return 0;
}
