#pragma once

#include "hdf5.h"

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
        for(size_t i=0; i<max_size; ++i) ptr[i] = 0.0;
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
