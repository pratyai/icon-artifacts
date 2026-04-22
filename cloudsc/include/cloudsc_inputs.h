#pragma once
#include <cstddef>
#include "h5_utils.h"

struct CloudSCData {
    int* ktype;
    int* ldcum;
    double* pa;
    double* pap;
    double* paph;
    double* pccn;
    double* pclv;
    double* pcovptot;
    double* pdyna;
    double* pdyni;
    double* pdynl;
    double* pfcqlng;
    double* pfcqnng;
    double* pfcqrng;
    double* pfcqsng;
    double* pfhpsl;
    double* pfhpsn;
    double* pfplsl;
    double* pfplsn;
    double* pfsqif;
    double* pfsqitur;
    double* pfsqlf;
    double* pfsqltur;
    double* pfsqrf;
    double* pfsqsf;
    double* phrlw;
    double* phrsw;
    double* picrit_aer;
    double* plcrit_aer;
    double* plsm;
    double* plu;
    double* plude;
    double* pmfd;
    double* pmfu;
    double* pnice;
    double* pq;
    double* prainfrac_toprfz;
    double* pre_ice;
    double* psnde;
    double* psupsat;
    double* pt;
    double* pvervel;
    double* pvfa;
    double* pvfi;
    double* pvfl;
    double* tendency_loc_a;
    double* tendency_loc_cld;
    double* tendency_loc_q;
    double* tendency_loc_t;
    double* tendency_tmp_a;
    double* tendency_tmp_cld;
    double* tendency_tmp_q;
    double* tendency_tmp_t;
};

// --- klon replication helpers ----------------------------------------------
// cloudsc's input HDF5 is written for some native klon (the benchmark dwarf
// uses klon=100).  For GPU perf profiling we often want a much larger
// effective klon so the grid has enough parallel work.  Rather than
// regenerate the HDF5, we load at the native klon and tile the horizontal
// dim by repeating columns modulo klon_native.

template <typename T>
static T* _tile_klon(T* orig, size_t orig_size, int klev, int klon_native,
                     int nclv, int klon_eff) {
    // Deduce the per-column "outer" size from the total element count.
    size_t outer;
    if      (orig_size == (size_t)klon_native)                    outer = 1;
    else if (orig_size == (size_t)klon_native * klev)             outer = (size_t)klev;
    else if (orig_size == (size_t)klon_native * (klev + 1))       outer = (size_t)klev + 1;
    else if (orig_size == (size_t)klon_native * klev * nclv)      outer = (size_t)klev * nclv;
    else if (orig_size == (size_t)klon_native * (klev + 1) * nclv) outer = (size_t)(klev + 1) * nclv;
    else return orig;  // unknown shape; leave untouched (caller mustn't tile it)

    T* out = new T[outer * (size_t)klon_eff];
    for (size_t o = 0; o < outer; ++o) {
        const T* row = orig + o * klon_native;
        T* dst = out + o * klon_eff;
        for (int j = 0; j < klon_eff; ++j)
            dst[j] = row[j % klon_native];
    }
    delete[] orig;
    return out;
}

inline CloudSCData load_inputs(hid_t file_id, int klon, int klev, int nclv) {
    CloudSCData d;
    size_t sz = (size_t)klon * (klev + 1) * nclv;
    d.ktype = load_h5_array_int(file_id, "KTYPE", sz);
    d.ldcum = load_h5_array_int(file_id, "LDCUM", sz);
    d.pa = load_h5_array_double(file_id, "PA", sz);
    d.pap = load_h5_array_double(file_id, "PAP", sz);
    d.paph = load_h5_array_double(file_id, "PAPH", sz);
    d.pccn = load_h5_array_double(file_id, "PCCN", sz);
    d.pclv = load_h5_array_double(file_id, "PCLV", sz);
    d.pcovptot = load_h5_array_double(file_id, "PCOVPTOT", sz);
    d.pdyna = load_h5_array_double(file_id, "PDYNA", sz);
    d.pdyni = load_h5_array_double(file_id, "PDYNI", sz);
    d.pdynl = load_h5_array_double(file_id, "PDYNL", sz);
    d.pfcqlng = load_h5_array_double(file_id, "PFCQLNG", sz);
    d.pfcqnng = load_h5_array_double(file_id, "PFCQNNG", sz);
    d.pfcqrng = load_h5_array_double(file_id, "PFCQRNG", sz);
    d.pfcqsng = load_h5_array_double(file_id, "PFCQSNG", sz);
    d.pfhpsl = load_h5_array_double(file_id, "PFHPSL", sz);
    d.pfhpsn = load_h5_array_double(file_id, "PFHPSN", sz);
    d.pfplsl = load_h5_array_double(file_id, "PFPLSL", sz);
    d.pfplsn = load_h5_array_double(file_id, "PFPLSN", sz);
    d.pfsqif = load_h5_array_double(file_id, "PFSQIF", sz);
    d.pfsqitur = load_h5_array_double(file_id, "PFSQITUR", sz);
    d.pfsqlf = load_h5_array_double(file_id, "PFSQLF", sz);
    d.pfsqltur = load_h5_array_double(file_id, "PFSQLTUR", sz);
    d.pfsqrf = load_h5_array_double(file_id, "PFSQRF", sz);
    d.pfsqsf = load_h5_array_double(file_id, "PFSQSF", sz);
    d.phrlw = load_h5_array_double(file_id, "PHRLW", sz);
    d.phrsw = load_h5_array_double(file_id, "PHRSW", sz);
    d.picrit_aer = load_h5_array_double(file_id, "PICRIT_AER", sz);
    d.plcrit_aer = load_h5_array_double(file_id, "PLCRIT_AER", sz);
    d.plsm = load_h5_array_double(file_id, "PLSM", sz);
    d.plu = load_h5_array_double(file_id, "PLU", sz);
    d.plude = load_h5_array_double(file_id, "PLUDE", sz);
    d.pmfd = load_h5_array_double(file_id, "PMFD", sz);
    d.pmfu = load_h5_array_double(file_id, "PMFU", sz);
    d.pnice = load_h5_array_double(file_id, "PNICE", sz);
    d.pq = load_h5_array_double(file_id, "PQ", sz);
    d.prainfrac_toprfz = load_h5_array_double(file_id, "PRAINFRAC_TOPRFZ", sz);
    d.pre_ice = load_h5_array_double(file_id, "PRE_ICE", sz);
    d.psnde = load_h5_array_double(file_id, "PSNDE", sz);
    d.psupsat = load_h5_array_double(file_id, "PSUPSAT", sz);
    d.pt = load_h5_array_double(file_id, "PT", sz);
    d.pvervel = load_h5_array_double(file_id, "PVERVEL", sz);
    d.pvfa = load_h5_array_double(file_id, "PVFA", sz);
    d.pvfi = load_h5_array_double(file_id, "PVFI", sz);
    d.pvfl = load_h5_array_double(file_id, "PVFL", sz);
    d.tendency_loc_a = load_h5_array_double(file_id, "TENDENCY_LOC_A", sz);
    d.tendency_loc_cld = load_h5_array_double(file_id, "TENDENCY_LOC_CLD", sz);
    d.tendency_loc_q = load_h5_array_double(file_id, "TENDENCY_LOC_Q", sz);
    d.tendency_loc_t = load_h5_array_double(file_id, "TENDENCY_LOC_T", sz);
    d.tendency_tmp_a = load_h5_array_double(file_id, "TENDENCY_TMP_A", sz);
    d.tendency_tmp_cld = load_h5_array_double(file_id, "TENDENCY_TMP_CLD", sz);
    d.tendency_tmp_q = load_h5_array_double(file_id, "TENDENCY_TMP_Q", sz);
    d.tendency_tmp_t = load_h5_array_double(file_id, "TENDENCY_TMP_T", sz);
    return d;
}

// Load native inputs and tile the horizontal dim so every array has
// klon_eff columns instead of klon_native.  Pure synthetic replication —
// useful for GPU perf profiling, NOT for physical-correctness runs.
inline CloudSCData load_inputs_tiled(hid_t file_id, int klon_native, int klev,
                                     int nclv, int klon_eff) {
    CloudSCData d = load_inputs(file_id, klon_native, klev, nclv);
    if (klon_eff == klon_native) return d;

    auto tile_d = [&](double*& p, size_t sz) {
        p = _tile_klon<double>(p, sz, klev, klon_native, nclv, klon_eff);
    };
    auto tile_i = [&](int*& p, size_t sz) {
        p = _tile_klon<int>(p, sz, klev, klon_native, nclv, klon_eff);
    };

    // Shapes per array (from save_outputs in this file):
    //   (klon,)                  : plsm, prainfrac_toprfz, ktype, ldcum
    //   (klev, klon)             : pa, pap, paph, pccn, pdyna, pdyni, pdynl,
    //                              phrlw, phrsw, picrit_aer, plcrit_aer, plu,
    //                              plude, pmfd, pmfu, pnice, pq, pre_ice,
    //                              psnde, psupsat, pt, pvervel, pvfa, pvfi,
    //                              pvfl, pcovptot, tendency_{loc,tmp}_{a,q,t}
    //   (klev+1, klon)           : pfcqlng, pfcqnng, pfcqrng, pfcqsng,
    //                              pfhpsl, pfhpsn, pfplsl, pfplsn,
    //                              pfsqif, pfsqitur, pfsqlf, pfsqltur,
    //                              pfsqrf, pfsqsf
    //   (nclv, klev, klon)       : pclv, tendency_loc_cld, tendency_tmp_cld

    size_t k1 = (size_t)klon_native;
    size_t k2 = (size_t)klon_native * klev;
    size_t k2p = (size_t)klon_native * (klev + 1);
    size_t k3 = (size_t)klon_native * klev * nclv;

    tile_i(d.ktype, k1);
    tile_i(d.ldcum, k1);
    tile_d(d.plsm, k1);
    tile_d(d.prainfrac_toprfz, k1);

    tile_d(d.pa, k2);         tile_d(d.pap, k2);        tile_d(d.paph, k2);
    tile_d(d.pccn, k2);       tile_d(d.pcovptot, k2);
    tile_d(d.pdyna, k2);      tile_d(d.pdyni, k2);      tile_d(d.pdynl, k2);
    tile_d(d.phrlw, k2);      tile_d(d.phrsw, k2);
    tile_d(d.picrit_aer, k2); tile_d(d.plcrit_aer, k2);
    tile_d(d.plu, k2);        tile_d(d.plude, k2);
    tile_d(d.pmfd, k2);       tile_d(d.pmfu, k2);
    tile_d(d.pnice, k2);      tile_d(d.pq, k2);
    tile_d(d.pre_ice, k2);    tile_d(d.psnde, k2);
    tile_d(d.psupsat, k2);    tile_d(d.pt, k2);
    tile_d(d.pvervel, k2);    tile_d(d.pvfa, k2);
    tile_d(d.pvfi, k2);       tile_d(d.pvfl, k2);
    tile_d(d.tendency_loc_a, k2); tile_d(d.tendency_loc_q, k2); tile_d(d.tendency_loc_t, k2);
    tile_d(d.tendency_tmp_a, k2); tile_d(d.tendency_tmp_q, k2); tile_d(d.tendency_tmp_t, k2);

    tile_d(d.pfcqlng, k2p);   tile_d(d.pfcqnng, k2p);
    tile_d(d.pfcqrng, k2p);   tile_d(d.pfcqsng, k2p);
    tile_d(d.pfhpsl, k2p);    tile_d(d.pfhpsn, k2p);
    tile_d(d.pfplsl, k2p);    tile_d(d.pfplsn, k2p);
    tile_d(d.pfsqif, k2p);    tile_d(d.pfsqitur, k2p);
    tile_d(d.pfsqlf, k2p);    tile_d(d.pfsqltur, k2p);
    tile_d(d.pfsqrf, k2p);    tile_d(d.pfsqsf, k2p);

    tile_d(d.pclv, k3);
    tile_d(d.tendency_loc_cld, k3);
    tile_d(d.tendency_tmp_cld, k3);

    return d;
}

inline void save_outputs(hid_t out_id, const CloudSCData& d, int klon, int klev, int nclv) {
    save_h5_array_int(out_id, "KTYPE", d.ktype, klon, klon);
    save_h5_array_int(out_id, "LDCUM", d.ldcum, klon, klon);
    save_h5_array_double(out_id, "PA", d.pa, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PAP", d.pap, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PAPH", d.paph, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PCCN", d.pccn, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PCLV", d.pclv, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PCOVPTOT", d.pcovptot, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PDYNA", d.pdyna, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PDYNI", d.pdyni, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PDYNL", d.pdynl, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PFCQLNG", d.pfcqlng, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFCQNNG", d.pfcqnng, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFCQRNG", d.pfcqrng, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFCQSNG", d.pfcqsng, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFHPSL", d.pfhpsl, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFHPSN", d.pfhpsn, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFPLSL", d.pfplsl, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFPLSN", d.pfplsn, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFSQIF", d.pfsqif, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFSQITUR", d.pfsqitur, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFSQLF", d.pfsqlf, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFSQLTUR", d.pfsqltur, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFSQRF", d.pfsqrf, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PFSQSF", d.pfsqsf, (size_t)klon * (klev + 1), klev, klon, nclv);
    save_h5_array_double(out_id, "PHRLW", d.phrlw, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PHRSW", d.phrsw, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PICRIT_AER", d.picrit_aer, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PLCRIT_AER", d.plcrit_aer, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PLSM", d.plsm, (size_t)klon, klev, klon, nclv);
    save_h5_array_double(out_id, "PLU", d.plu, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PLUDE", d.plude, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PMFD", d.pmfd, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PMFU", d.pmfu, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PNICE", d.pnice, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PQ", d.pq, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PRAINFRAC_TOPRFZ", d.prainfrac_toprfz, (size_t)klon, klev, klon, nclv);
    save_h5_array_double(out_id, "PRE_ICE", d.pre_ice, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PSNDE", d.psnde, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PSUPSAT", d.psupsat, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PT", d.pt, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PVERVEL", d.pvervel, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PVFA", d.pvfa, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PVFI", d.pvfi, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "PVFL", d.pvfl, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "TENDENCY_LOC_A", d.tendency_loc_a, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "TENDENCY_LOC_CLD", d.tendency_loc_cld, (size_t)klon * klev * nclv, klev, klon, nclv);
    save_h5_array_double(out_id, "TENDENCY_LOC_Q", d.tendency_loc_q, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "TENDENCY_LOC_T", d.tendency_loc_t, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "TENDENCY_TMP_A", d.tendency_tmp_a, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "TENDENCY_TMP_CLD", d.tendency_tmp_cld, (size_t)klon * klev * nclv, klev, klon, nclv);
    save_h5_array_double(out_id, "TENDENCY_TMP_Q", d.tendency_tmp_q, (size_t)klon * klev, klev, klon, nclv);
    save_h5_array_double(out_id, "TENDENCY_TMP_T", d.tendency_tmp_t, (size_t)klon * klev, klev, klon, nclv);
}

inline void free_inputs(CloudSCData& d) {
    delete[] d.ktype; delete[] d.ldcum;
    delete[] d.pa; delete[] d.pap; delete[] d.paph; delete[] d.pccn; delete[] d.pclv;
    delete[] d.pcovptot; delete[] d.pdyna; delete[] d.pdyni; delete[] d.pdynl;
    delete[] d.pfcqlng; delete[] d.pfcqnng; delete[] d.pfcqrng; delete[] d.pfcqsng;
    delete[] d.pfhpsl; delete[] d.pfhpsn; delete[] d.pfplsl; delete[] d.pfplsn;
    delete[] d.pfsqif; delete[] d.pfsqitur; delete[] d.pfsqlf; delete[] d.pfsqltur;
    delete[] d.pfsqrf; delete[] d.pfsqsf; delete[] d.phrlw; delete[] d.phrsw;
    delete[] d.picrit_aer; delete[] d.plcrit_aer; delete[] d.plsm; delete[] d.plu;
    delete[] d.plude; delete[] d.pmfd; delete[] d.pmfu; delete[] d.pnice; delete[] d.pq;
    delete[] d.prainfrac_toprfz; delete[] d.pre_ice; delete[] d.psnde; delete[] d.psupsat;
    delete[] d.pt; delete[] d.pvervel; delete[] d.pvfa; delete[] d.pvfi; delete[] d.pvfl;
    delete[] d.tendency_loc_a; delete[] d.tendency_loc_cld;
    delete[] d.tendency_loc_q; delete[] d.tendency_loc_t;
    delete[] d.tendency_tmp_a; delete[] d.tendency_tmp_cld;
    delete[] d.tendency_tmp_q; delete[] d.tendency_tmp_t;
}
