
#include <iostream>
#include <vector>
#include <chrono>
#include <string>
#include <cstring>
#include <filesystem>
#include <cuda_runtime.h>
#include "hdf5.h"
#include "codegen/cloudsc_py.h"

double* load_h5_array_double(hid_t file_id, std::string name, size_t max_size) {
    double* ptr = new double[max_size];
    for(size_t i=0; i<max_size; ++i) ptr[i] = 0.0;

    if (file_id >= 0 && H5Lexists(file_id, name.c_str(), H5P_DEFAULT) > 0) {
        hid_t dataset_id = H5Dopen2(file_id, name.c_str(), H5P_DEFAULT);
        hid_t space_id = H5Dget_space(dataset_id);
        hssize_t num_elements = H5Sget_simple_extent_npoints(space_id);
        H5Dread(dataset_id, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, ptr);
        H5Sclose(space_id);
        H5Dclose(dataset_id);
    } else {
        for(size_t i=0; i<max_size; ++i) ptr[i] = 0.5;
    }
    return ptr;
}

int* load_h5_array_int(hid_t file_id, std::string name, size_t max_size) {
    int* ptr = new int[max_size];
    for(size_t i=0; i<max_size; ++i) ptr[i] = 0;

    if (file_id >= 0 && H5Lexists(file_id, name.c_str(), H5P_DEFAULT) > 0) {
        hid_t dataset_id = H5Dopen2(file_id, name.c_str(), H5P_DEFAULT);
        hid_t space_id = H5Dget_space(dataset_id);
        H5Dread(dataset_id, H5T_NATIVE_INT, H5S_ALL, H5S_ALL, H5P_DEFAULT, ptr);
        H5Sclose(space_id);
        H5Dclose(dataset_id);
    } else {
        for(size_t i=0; i<max_size; ++i) ptr[i] = 1;
    }
    return ptr;
}

void save_h5_array_double(hid_t file_id, std::string name, double* ptr, size_t size, int klev, int klon, int nclv) {
    hsize_t dims[3];
    int rank = 0;
    if (size == (size_t)klon * (klev + 1)) { dims[0] = klev + 1; dims[1] = klon; rank = 2; }
    else if (size == (size_t)klon * klev) { dims[0] = klev; dims[1] = klon; rank = 2; }
    else if (size == (size_t)klon * klev * nclv) { dims[0] = nclv; dims[1] = klev; dims[2] = klon; rank = 3; }
    else if (size == (size_t)klon) { dims[0] = klon; rank = 1; }
    else { dims[0] = size; rank = 1; }
    hid_t space_id = H5Screate_simple(rank, dims, NULL);
    hid_t dataset_id = H5Dcreate2(file_id, name.c_str(), H5T_NATIVE_DOUBLE, space_id, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    H5Dwrite(dataset_id, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, ptr);
    H5Dclose(dataset_id);
    H5Sclose(space_id);
}

void save_h5_array_int(hid_t file_id, std::string name, int* ptr, size_t size, int klon) {
    hsize_t dims[1] = { (hsize_t)size };
    hid_t space_id = H5Screate_simple(1, dims, NULL);
    hid_t dataset_id = H5Dcreate2(file_id, name.c_str(), H5T_NATIVE_INT, space_id, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    H5Dwrite(dataset_id, H5T_NATIVE_INT, H5S_ALL, H5S_ALL, H5P_DEFAULT, ptr);
    H5Dclose(dataset_id);
    H5Sclose(space_id);
}

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

    // Constants
    double ydcst_rcpd = 1004.70885783306744;
    double ydcst_rd = 287.059673666590697;
    double ydcst_retv = 0.607766731611463662;
    double ydcst_rg = 9.80664999999999942;
    double ydcst_rlmlt = 333700.0;
    double ydcst_rlstt = 2834500.0;
    double ydcst_rlvtt = 2500800.0;
    double ydcst_rtt = 273.160000000000025;
    double ydcst_rv = 461.524993308387877;
    double ydthf_r2es = 380.160870344284717;
    double ydthf_r3ies = 22.5869999999999997;
    double ydthf_r3les = 17.5019999999999989;
    double ydthf_r4ies = -0.699999999999999956;
    double ydthf_r4les = 32.1899999999999977;
    double ydthf_r5alscp = 17451123.2533625774;
    double ydthf_r5alvcp = 10497584.6816953104;
    double ydthf_r5ies = 6185.67582000000039;
    double ydthf_r5les = 4217.45694000000003;
    double ydthf_ralfdcp = 332.13601870666929;
    double ydthf_ralsdcp = 2821.21529824409345;
    double ydthf_ralvdcp = 2489.07927953742455;
    double ydthf_rkoop1 = 2.58300000000000018;
    double ydthf_rkoop2 = 0.00481160000000000008;
    double ydthf_rtice = 250.160000000000025;
    double ydthf_rticecu = 250.160000000000025;
    double ydthf_rtwat = 273.160000000000025;
    double ydthf_rtwat_rtice_r = 0.0434782608695652162;
    double ydthf_rtwat_rticecu_r = 0.0434782608695652162;
    int yrecldp_laericeauto = false;
    int yrecldp_laericesed = false;
    int yrecldp_laerliqautolsp = false;
    int yrecldp_laerliqcoll = false;
    int yrecldp_ncldtop = 15;
    int yrecldp_nssopt = 1;
    double yrecldp_ramid = 0.800000000000000044;
    double yrecldp_ramin = 1.00000000000000002e-08;
    double yrecldp_rccn = 125.0;
    double yrecldp_rcl_apb1 = 714000000000.0;
    double yrecldp_rcl_apb2 = 116000000.0;
    double yrecldp_rcl_apb3 = 241.599999999999994;
    double yrecldp_rcl_cdenom1 = 557000000000.0;
    double yrecldp_rcl_cdenom2 = 103000000.0;
    double yrecldp_rcl_cdenom3 = 204.0;
    double yrecldp_rcl_const1i = 3.62318801151369977e-06;
    double yrecldp_rcl_const1r = 1.38230076757950893;
    double yrecldp_rcl_const1s = 3.62318801151369977e-06;
    double yrecldp_rcl_const2i = 6283185.30717958603;
    double yrecldp_rcl_const2r = 2143.2299120517614;
    double yrecldp_rcl_const2s = 6283185.30717958603;
    double yrecldp_rcl_const3i = 596.999847583599831;
    double yrecldp_rcl_const3r = 0.634999999999999787;
    double yrecldp_rcl_const3s = 596.999847583599831;
    double yrecldp_rcl_const4i = 0.66666666666666663;
    double yrecldp_rcl_const4r = -0.200000000000000178;
    double yrecldp_rcl_const4s = 0.66666666666666663;
    double yrecldp_rcl_const5i = 0.921166666666666689;
    double yrecldp_rcl_const5r = 8685252.96508213319;
    double yrecldp_rcl_const5s = 0.921166666666666689;
    double yrecldp_rcl_const6i = 1.00000009489611852;
    double yrecldp_rcl_const6r = -4.79999999999999982;
    double yrecldp_rcl_const6s = 1.00000009489611852;
    double yrecldp_rcl_const7s = 90363515.7635107338;
    double yrecldp_rcl_const8s = 1.17566666666666664;
    double yrecldp_rcl_fac1 = 4146.9027898470631;
    double yrecldp_rcl_fac2 = 0.55555555555555558;
    double yrecldp_rcl_fzrab = -0.660000000000000031;
    double yrecldp_rcl_ka273 = 0.0240000000000000005;
    double yrecldp_rcl_kk_cloud_num_land = 300.0;
    double yrecldp_rcl_kk_cloud_num_sea = 50.0;
    double yrecldp_rcl_kkaac = 67.0;
    double yrecldp_rcl_kkaau = 1350.0;
    double yrecldp_rcl_kkbac = 1.14999999999999991;
    double yrecldp_rcl_kkbaun = -1.79000000000000004;
    double yrecldp_rcl_kkbauq = 2.4700000000000002;
    double yrecldp_rclcrit_land = 0.000550000000000000033;
    double yrecldp_rclcrit_sea = 0.000250000000000000005;
    double yrecldp_rcldiff = 3.00000000000000008e-06;
    double yrecldp_rcldiff_convi = 7.0;
    double yrecldp_rcldtopcf = 0.0100000000000000002;
    double yrecldp_rcovpmin = 0.100000000000000006;
    double yrecldp_rdensref = 1.0;
    double yrecldp_rdepliqrefdepth = 500.0;
    double yrecldp_rdepliqrefrate = 0.100000000000000006;
    double yrecldp_riceinit = 9.9999999999999998e-13;
    double yrecldp_rkconv = 0.000166666666666666661;
    double yrecldp_rkooptau = 10800.0;
    double yrecldp_rlcritsnow = 3.00000000000000008e-05;
    double yrecldp_rlmin = 1.00000000000000002e-08;
    double yrecldp_rnice = 0.0269999999999999997;
    double yrecldp_rpecons = 5.54725619859993018e-05;
    double yrecldp_rprc1 = 100.0;
    double yrecldp_rprecrhmax = 0.699999999999999956;
    double yrecldp_rsnowlin1 = 0.00100000000000000002;
    double yrecldp_rsnowlin2 = 0.0299999999999999989;
    double yrecldp_rtaumel = 7200.0;
    double yrecldp_rthomo = 235.160000000000025;
    double yrecldp_rvice = 0.130000000000000004;
    double yrecldp_rvrain = 4.0;
    double yrecldp_rvrfactor = 0.00508999999999999991;
    double yrecldp_rvsnow = 1.0;

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

    int* ktype = load_h5_array_int(file_id, "KTYPE", klon * (klev + 1) * nclv);
    int* ldcum = load_h5_array_int(file_id, "LDCUM", klon * (klev + 1) * nclv);
    double* pa = load_h5_array_double(file_id, "PA", klon * (klev + 1) * nclv);
    double* pap = load_h5_array_double(file_id, "PAP", klon * (klev + 1) * nclv);
    double* paph = load_h5_array_double(file_id, "PAPH", klon * (klev + 1) * nclv);
    double* pccn = load_h5_array_double(file_id, "PCCN", klon * (klev + 1) * nclv);
    double* pclv = load_h5_array_double(file_id, "PCLV", klon * (klev + 1) * nclv);
    double* pcovptot = load_h5_array_double(file_id, "PCOVPTOT", klon * (klev + 1) * nclv);
    double* pdyna = load_h5_array_double(file_id, "PDYNA", klon * (klev + 1) * nclv);
    double* pdyni = load_h5_array_double(file_id, "PDYNI", klon * (klev + 1) * nclv);
    double* pdynl = load_h5_array_double(file_id, "PDYNL", klon * (klev + 1) * nclv);
    double* pfcqlng = load_h5_array_double(file_id, "PFCQLNG", klon * (klev + 1) * nclv);
    double* pfcqnng = load_h5_array_double(file_id, "PFCQNNG", klon * (klev + 1) * nclv);
    double* pfcqrng = load_h5_array_double(file_id, "PFCQRNG", klon * (klev + 1) * nclv);
    double* pfcqsng = load_h5_array_double(file_id, "PFCQSNG", klon * (klev + 1) * nclv);
    double* pfhpsl = load_h5_array_double(file_id, "PFHPSL", klon * (klev + 1) * nclv);
    double* pfhpsn = load_h5_array_double(file_id, "PFHPSN", klon * (klev + 1) * nclv);
    double* pfplsl = load_h5_array_double(file_id, "PFPLSL", klon * (klev + 1) * nclv);
    double* pfplsn = load_h5_array_double(file_id, "PFPLSN", klon * (klev + 1) * nclv);
    double* pfsqif = load_h5_array_double(file_id, "PFSQIF", klon * (klev + 1) * nclv);
    double* pfsqitur = load_h5_array_double(file_id, "PFSQITUR", klon * (klev + 1) * nclv);
    double* pfsqlf = load_h5_array_double(file_id, "PFSQLF", klon * (klev + 1) * nclv);
    double* pfsqltur = load_h5_array_double(file_id, "PFSQLTUR", klon * (klev + 1) * nclv);
    double* pfsqrf = load_h5_array_double(file_id, "PFSQRF", klon * (klev + 1) * nclv);
    double* pfsqsf = load_h5_array_double(file_id, "PFSQSF", klon * (klev + 1) * nclv);
    double* phrlw = load_h5_array_double(file_id, "PHRLW", klon * (klev + 1) * nclv);
    double* phrsw = load_h5_array_double(file_id, "PHRSW", klon * (klev + 1) * nclv);
    double* picrit_aer = load_h5_array_double(file_id, "PICRIT_AER", klon * (klev + 1) * nclv);
    double* plcrit_aer = load_h5_array_double(file_id, "PLCRIT_AER", klon * (klev + 1) * nclv);
    double* plsm = load_h5_array_double(file_id, "PLSM", klon * (klev + 1) * nclv);
    double* plu = load_h5_array_double(file_id, "PLU", klon * (klev + 1) * nclv);
    double* plude = load_h5_array_double(file_id, "PLUDE", klon * (klev + 1) * nclv);
    double* pmfd = load_h5_array_double(file_id, "PMFD", klon * (klev + 1) * nclv);
    double* pmfu = load_h5_array_double(file_id, "PMFU", klon * (klev + 1) * nclv);
    double* pnice = load_h5_array_double(file_id, "PNICE", klon * (klev + 1) * nclv);
    double* pq = load_h5_array_double(file_id, "PQ", klon * (klev + 1) * nclv);
    double* prainfrac_toprfz = load_h5_array_double(file_id, "PRAINFRAC_TOPRFZ", klon * (klev + 1) * nclv);
    double* pre_ice = load_h5_array_double(file_id, "PRE_ICE", klon * (klev + 1) * nclv);
    double* psnde = load_h5_array_double(file_id, "PSNDE", klon * (klev + 1) * nclv);
    double* psupsat = load_h5_array_double(file_id, "PSUPSAT", klon * (klev + 1) * nclv);
    double* pt = load_h5_array_double(file_id, "PT", klon * (klev + 1) * nclv);
    double* pvervel = load_h5_array_double(file_id, "PVERVEL", klon * (klev + 1) * nclv);
    double* pvfa = load_h5_array_double(file_id, "PVFA", klon * (klev + 1) * nclv);
    double* pvfi = load_h5_array_double(file_id, "PVFI", klon * (klev + 1) * nclv);
    double* pvfl = load_h5_array_double(file_id, "PVFL", klon * (klev + 1) * nclv);
    double* tendency_loc_a = load_h5_array_double(file_id, "TENDENCY_LOC_A", klon * (klev + 1) * nclv);
    double* tendency_loc_cld = load_h5_array_double(file_id, "TENDENCY_LOC_CLD", klon * (klev + 1) * nclv);
    double* tendency_loc_q = load_h5_array_double(file_id, "TENDENCY_LOC_Q", klon * (klev + 1) * nclv);
    double* tendency_loc_t = load_h5_array_double(file_id, "TENDENCY_LOC_T", klon * (klev + 1) * nclv);
    double* tendency_tmp_a = load_h5_array_double(file_id, "TENDENCY_TMP_A", klon * (klev + 1) * nclv);
    double* tendency_tmp_cld = load_h5_array_double(file_id, "TENDENCY_TMP_CLD", klon * (klev + 1) * nclv);
    double* tendency_tmp_q = load_h5_array_double(file_id, "TENDENCY_TMP_Q", klon * (klev + 1) * nclv);
    double* tendency_tmp_t = load_h5_array_double(file_id, "TENDENCY_TMP_T", klon * (klev + 1) * nclv);

    if (file_id >= 0) H5Fclose(file_id);

    // NOTE: __dace_init args depend on which symbols are free in the GPU SDFG.
    // If ncldq* were baked in by the unroll pass, use (klev, klon, nclv).
    // Otherwise match the generated cloudsc_py.h header.
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
            save_h5_array_int(out_id, "KTYPE", ktype, klon, klon);
            save_h5_array_int(out_id, "LDCUM", ldcum, klon, klon);
            save_h5_array_double(out_id, "PA", pa, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PAP", pap, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PAPH", paph, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PCCN", pccn, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PCLV", pclv, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PCOVPTOT", pcovptot, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PDYNA", pdyna, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PDYNI", pdyni, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PDYNL", pdynl, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PFCQLNG", pfcqlng, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFCQNNG", pfcqnng, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFCQRNG", pfcqrng, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFCQSNG", pfcqsng, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFHPSL", pfhpsl, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFHPSN", pfhpsn, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFPLSL", pfplsl, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFPLSN", pfplsn, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFSQIF", pfsqif, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFSQITUR", pfsqitur, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFSQLF", pfsqlf, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFSQLTUR", pfsqltur, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFSQRF", pfsqrf, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PFSQSF", pfsqsf, klon * (klev + 1), klev, klon, nclv);
            save_h5_array_double(out_id, "PHRLW", phrlw, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PHRSW", phrsw, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PICRIT_AER", picrit_aer, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PLCRIT_AER", plcrit_aer, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PLSM", plsm, klon, klev, klon, nclv);
            save_h5_array_double(out_id, "PLU", plu, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PLUDE", plude, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PMFD", pmfd, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PMFU", pmfu, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PNICE", pnice, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PQ", pq, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PRAINFRAC_TOPRFZ", prainfrac_toprfz, klon, klev, klon, nclv);
            save_h5_array_double(out_id, "PRE_ICE", pre_ice, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PSNDE", psnde, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PSUPSAT", psupsat, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PT", pt, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PVERVEL", pvervel, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PVFA", pvfa, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PVFI", pvfi, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "PVFL", pvfl, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "TENDENCY_LOC_A", tendency_loc_a, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "TENDENCY_LOC_CLD", tendency_loc_cld, klon * klev * nclv, klev, klon, nclv);
            save_h5_array_double(out_id, "TENDENCY_LOC_Q", tendency_loc_q, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "TENDENCY_LOC_T", tendency_loc_t, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "TENDENCY_TMP_A", tendency_tmp_a, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "TENDENCY_TMP_CLD", tendency_tmp_cld, klon * klev * nclv, klev, klon, nclv);
            save_h5_array_double(out_id, "TENDENCY_TMP_Q", tendency_tmp_q, klon * klev, klev, klon, nclv);
            save_h5_array_double(out_id, "TENDENCY_TMP_T", tendency_tmp_t, klon * klev, klev, klon, nclv);
            H5Fclose(out_id);
            std::cout << "Saved results to: " << full_path << std::endl;
        }
    }

    std::cout << "Total execution time: " << total_time << "s" << std::endl;
    std::cout << "Average time per step: " << total_time / num_steps << "s" << std::endl;

    __dace_exit_cloudsc_py(handle);

    delete[] ktype; delete[] ldcum;
    delete[] pa; delete[] pap; delete[] paph; delete[] pccn; delete[] pclv;
    delete[] pcovptot; delete[] pdyna; delete[] pdyni; delete[] pdynl;
    delete[] pfcqlng; delete[] pfcqnng; delete[] pfcqrng; delete[] pfcqsng;
    delete[] pfhpsl; delete[] pfhpsn; delete[] pfplsl; delete[] pfplsn;
    delete[] pfsqif; delete[] pfsqitur; delete[] pfsqlf; delete[] pfsqltur;
    delete[] pfsqrf; delete[] pfsqsf; delete[] phrlw; delete[] phrsw;
    delete[] picrit_aer; delete[] plcrit_aer; delete[] plsm; delete[] plu;
    delete[] plude; delete[] pmfd; delete[] pmfu; delete[] pnice; delete[] pq;
    delete[] prainfrac_toprfz; delete[] pre_ice; delete[] psnde; delete[] psupsat;
    delete[] pt; delete[] pvervel; delete[] pvfa; delete[] pvfi; delete[] pvfl;
    delete[] tendency_loc_a; delete[] tendency_loc_cld;
    delete[] tendency_loc_q; delete[] tendency_loc_t;
    delete[] tendency_tmp_a; delete[] tendency_tmp_cld;
    delete[] tendency_tmp_q; delete[] tendency_tmp_t;

    std::cout << "Done." << std::endl;
    return 0;
}
