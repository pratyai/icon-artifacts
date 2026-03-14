import dace
import numpy as np
import os
import shutil
import re
from pathlib import Path

from dace.codegen import codegen, compiler
from dace.sdfg import infer_types

dace.config.Config.set("compiler", "default_data_types", value="C")


# --- Source Management (matches velocity/utils/compile_if_propagated_sdfgs.py) ---


def repl_in_file(file_path: str, src: str, dst: str):
    with open(file_path, "r") as f:
        code = f.read()
    with open(file_path, "w") as f:
        f.write(code.replace(src, dst))


def modify_files_in_directory(directory):
    pattern = re.compile(r"^(\s*)int tmp_struct_symbol")
    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            if file_path.endswith((".c", ".h", ".cpp", ".cu")):
                modify_file(file_path, pattern)


def modify_file(file_path, pattern):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    modified = False
    new_lines = []
    for line in lines:
        if pattern.match(line) and "(" not in line and "," not in line and ";" in line:
            line = pattern.sub(r"\1static int tmp_struct_symbol", line)
            modified = True
        new_lines.append(line)

    if modified:
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)


def flatten_build_folder(build_loc: Path, sdfg_name: str) -> tuple[str, str]:
    """
    Moves necessary files to the parent of build_loc and removes build_loc.
    Matches velocity's flatten_build_folder pattern.
    """
    cpu_path = None
    cpu_ext = "cpp"
    for ext in ["cpp", "cc"]:
        p = build_loc / "src" / "cpu" / f"{sdfg_name}.{ext}"
        if p.exists():
            cpu_path = p
            cpu_ext = ext
            break

    header_path = build_loc / "include" / f"{sdfg_name}.h"
    hash_path = build_loc / "include" / "hash.h"

    target_cpu = build_loc.parent / f"{sdfg_name}.{cpu_ext}"
    target_header = build_loc.parent / f"{sdfg_name}.h"
    target_hash = build_loc.parent / "hash.h"

    if cpu_path and cpu_path.exists():
        shutil.move(str(cpu_path), str(target_cpu))
    else:
        raise FileNotFoundError(
            f"Could not find {sdfg_name}.cpp in {build_loc / 'src' / 'cpu'}"
        )
    if header_path.exists():
        shutil.move(str(header_path), str(target_header))
    if hash_path.exists():
        shutil.move(str(hash_path), str(target_hash))

    # Clean up the original build folder
    shutil.rmtree(build_loc, ignore_errors=True)

    # Patch includes
    repl_in_file(str(target_cpu), "../../include/hash.h", "hash.h")
    repl_in_file(str(target_cpu), f"../../include/{sdfg_name}.h", f"{sdfg_name}.h")

    return str(target_cpu), str(target_header)


# --- Main driver generation ---


def generate_main_cpp(header_path: Path, sdfg: dace.SDFG):
    with open(header_path, "r") as f:
        content = f.read()

    match = re.search(r'void __program_cloudsc_py\((.*?)\);', content, re.DOTALL)
    if not match:
        raise ValueError("Could not find __program_cloudsc_py in header")

    args_str = match.group(1).replace('\n', ' ')
    args = [a.strip() for a in args_str.split(',')]

    allocs = []
    const_defs = []
    call_args = []

    predefined = ['klon', 'klev', 'nclv', 'ncldqi', 'ncldql', 'ncldqr', 'ncldqs', 'ncldqv', 'kidia', 'kfdia', 'ptsphy']

    sdfg_constants = {}
    for s in sdfg.all_sdfgs_recursive():
        sdfg_constants.update(s.constants)

    if os.path.exists("input.h5"):
        import h5py
        with h5py.File("input.h5", 'r') as f:
            for k in f.keys():
                if f[k].shape == (1,):
                    sdfg_constants[k.lower()] = f[k][0]

    for arg in args:
        parts = arg.split()
        if not parts: continue
        arg_type = " ".join(parts[:-1]).replace("__restrict__", "").strip()
        arg_name = parts[-1]

        if arg_name == "handle":
            call_args.append("handle")
            continue

        if '*' in arg_type:
            base_type = arg_type.replace('*', '').strip()
            size_expr = "klon * (klev + 1) * nclv"
            if base_type == "double":
                allocs.append(f"    double* {arg_name} = load_h5_array_double(file_id, \"{arg_name.upper()}\", {size_expr});")
            else:
                allocs.append(f"    int* {arg_name} = load_h5_array_int(file_id, \"{arg_name.upper()}\", {size_expr});")
            call_args.append(arg_name)
        else:
            if arg_name in predefined:
                call_args.append(arg_name)
            else:
                lookup_names = [arg_name, arg_name.upper()]
                for prefix in ['ydcst_', 'ydthf_', 'yrecldp_']:
                    if arg_name.startswith(prefix):
                        short_name = arg_name[len(prefix):]
                        lookup_names.append(short_name)
                        lookup_names.append(short_name.upper())

                val = None
                for ln in lookup_names:
                    if ln in sdfg_constants:
                        val = sdfg_constants[ln]
                        break
                if val is None: val = 0.0

                if isinstance(val, (bool, np.bool_)):
                    val_str = "true" if val else "false"
                elif isinstance(val, (float, np.floating)):
                    val_str = f"{val:.18g}"
                    if '.' not in val_str and 'e' not in val_str:
                        val_str += ".0"
                else:
                    val_str = str(val)
                const_defs.append(f"    {arg_type} {arg_name} = {val_str};")
                call_args.append(arg_name)

    klon = int(sdfg_constants.get('klon', 100))
    klev = int(sdfg_constants.get('klev', 137))
    nclv = int(sdfg_constants.get('nclv', 5))
    ncldqi = int(sdfg_constants.get('ncldqi', 2))
    ncldql = int(sdfg_constants.get('ncldql', 1))
    ncldqr = int(sdfg_constants.get('ncldqr', 3))
    ncldqs = int(sdfg_constants.get('ncldqs', 4))
    ncldqv = int(sdfg_constants.get('ncldqv', 5))
    kidia = int(sdfg_constants.get('kidia', 1))
    kfdia = int(sdfg_constants.get('kfdia', klon))
    ptsphy = float(sdfg_constants.get('ptsphy', 3600.0))

    const_defs_str = "\n".join(const_defs)
    allocs_str = "\n".join(allocs)
    call_args_str = ", ".join(call_args)

    save_calls = []
    for arg in args:
        parts = arg.split()
        if not parts: continue
        arg_type = " ".join(parts[:-1]).replace("__restrict__", "").strip()
        arg_name = parts[-1]
        if '*' in arg_type:
            if arg_name == 'prainfrac_toprfz' or arg_name == 'plsm' or arg_name == 'ldcum' or arg_name == 'ktype':
                size_expr = "klon"
            elif arg_name == 'pclv' or arg_name.startswith('tendency_'):
                if '_cld' in arg_name: size_expr = "klon * klev * nclv"
                else: size_expr = "klon * klev"
            elif arg_name.startswith('pf'):
                size_expr = "klon * (klev + 1)"
            else:
                size_expr = "klon * klev"

            if arg_type.replace('*','').strip() == "double":
                save_calls.append(f"            save_h5_array_double(out_id, \"{arg_name.upper()}\", {arg_name}, {size_expr}, klev, klon, nclv);")
            else:
                save_calls.append(f"            save_h5_array_int(out_id, \"{arg_name.upper()}\", {arg_name}, {size_expr}, klon);")
    save_calls_str = "\n".join(save_calls)

    main_content = f"""
#include <iostream>
#include <vector>
#include <chrono>
#include <string>
#include <filesystem>
#include "hdf5.h"
#include "codegen/cloudsc_py.h"

double* load_h5_array_double(hid_t file_id, std::string name, size_t max_size) {{
    double* ptr = new double[max_size];
    for(size_t i=0; i<max_size; ++i) ptr[i] = 0.0;

    if (file_id >= 0 && H5Lexists(file_id, name.c_str(), H5P_DEFAULT) > 0) {{
        hid_t dataset_id = H5Dopen2(file_id, name.c_str(), H5P_DEFAULT);
        hid_t space_id = H5Dget_space(dataset_id);
        hssize_t num_elements = H5Sget_simple_extent_npoints(space_id);
        H5Dread(dataset_id, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, ptr);
        H5Sclose(space_id);
        H5Dclose(dataset_id);
    }} else {{
        for(size_t i=0; i<max_size; ++i) ptr[i] = 0.5;
    }}
    return ptr;
}}

int* load_h5_array_int(hid_t file_id, std::string name, size_t max_size) {{
    int* ptr = new int[max_size];
    for(size_t i=0; i<max_size; ++i) ptr[i] = 0;

    if (file_id >= 0 && H5Lexists(file_id, name.c_str(), H5P_DEFAULT) > 0) {{
        hid_t dataset_id = H5Dopen2(file_id, name.c_str(), H5P_DEFAULT);
        hid_t space_id = H5Dget_space(dataset_id);
        H5Dread(dataset_id, H5T_NATIVE_INT, H5S_ALL, H5S_ALL, H5P_DEFAULT, ptr);
        H5Sclose(space_id);
        H5Dclose(dataset_id);
    }} else {{
        for(size_t i=0; i<max_size; ++i) ptr[i] = 1;
    }}
    return ptr;
}}

void save_h5_array_double(hid_t file_id, std::string name, double* ptr, size_t size, int klev, int klon, int nclv) {{
    hsize_t dims[3];
    int rank = 0;
    if (size == (size_t)klon * (klev + 1)) {{ dims[0] = klev + 1; dims[1] = klon; rank = 2; }}
    else if (size == (size_t)klon * klev) {{ dims[0] = klev; dims[1] = klon; rank = 2; }}
    else if (size == (size_t)klon * klev * nclv) {{ dims[0] = nclv; dims[1] = klev; dims[2] = klon; rank = 3; }}
    else if (size == (size_t)klon) {{ dims[0] = klon; rank = 1; }}
    else {{ dims[0] = size; rank = 1; }}

    hid_t space_id = H5Screate_simple(rank, dims, NULL);
    hid_t dataset_id = H5Dcreate2(file_id, name.c_str(), H5T_NATIVE_DOUBLE, space_id, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    H5Dwrite(dataset_id, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, ptr);
    H5Dclose(dataset_id);
    H5Sclose(space_id);
}}

void save_h5_array_int(hid_t file_id, std::string name, int* ptr, size_t size, int klon) {{
    hsize_t dims[1] = {{ (hsize_t)size }};
    hid_t space_id = H5Screate_simple(1, dims, NULL);
    hid_t dataset_id = H5Dcreate2(file_id, name.c_str(), H5T_NATIVE_INT, space_id, H5P_DEFAULT, H5P_DEFAULT, H5P_DEFAULT);
    H5Dwrite(dataset_id, H5T_NATIVE_INT, H5S_ALL, H5S_ALL, H5P_DEFAULT, ptr);
    H5Dclose(dataset_id);
    H5Sclose(space_id);
}}

int main(int argc, char** argv) {{
    int num_steps = 1;
    if (argc > 1) num_steps = std::stoi(argv[1]);
    bool save_output = false;
    for(int i=1; i<argc; ++i) if(std::string(argv[i]) == "--save") save_output = true;

    int klon = {klon}, klev = {klev}, nclv = {nclv};
    int ncldqi = {ncldqi}, ncldql = {ncldql}, ncldqr = {ncldqr}, ncldqs = {ncldqs}, ncldqv = {ncldqv};
    int kidia = {kidia}, kfdia = {kfdia};
    double ptsphy = {ptsphy:g};

    std::string input_file = "input.h5";
    hid_t file_id = -1;
    if (std::filesystem::exists(input_file)) {{
        std::cout << "Reading input data from: " << input_file << std::endl;
        file_id = H5Fopen(input_file.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
    }} else {{
        std::cerr << "Warning: " << input_file << " not found, using dummy data." << std::endl;
    }}

    std::cout << "Running CloudSC for " << num_steps << " steps." << std::endl;

    // Constants
{const_defs_str}

    // Array allocations & loading
{allocs_str}

    if (file_id >= 0) H5Fclose(file_id);

    std::cout << "Initializing DaCe CloudSC CPU program (klon=" << klon << ", klev=" << klev << ")..." << std::endl;
    auto *handle = __dace_init_cloudsc_py(klev, klon, ncldqi, ncldql, ncldqr, ncldqs, ncldqv, nclv);

    std::cout << "Executing CloudSC..." << std::endl;
    double total_time = 0;
    for (int s=0; s<num_steps; ++s) {{
        auto start = std::chrono::high_resolution_clock::now();
        __program_cloudsc_py({call_args_str});
        auto end = std::chrono::high_resolution_clock::now();
        total_time += std::chrono::duration<double>(end - start).count();

        if (save_output) {{
            std::filesystem::create_directories("outputs_cpp");
            std::string out_name = "outputs_cpp/cpp_output_step_" + std::to_string(s) + ".h5";
            std::filesystem::path full_path = std::filesystem::absolute(out_name);
            hid_t out_id = H5Fcreate(out_name.c_str(), H5F_ACC_TRUNC, H5P_DEFAULT, H5P_DEFAULT);
{save_calls_str}
            H5Fclose(out_id);
            std::cout << "Saved results to: " << full_path << std::endl;
        }}
    }}

    std::cout << "Total execution time: " << total_time << "s" << std::endl;
    std::cout << "Average time per step: " << total_time / num_steps << "s" << std::endl;

    __dace_exit_cloudsc_py(handle);
    return 0;
}}
"""
    with open("cloudsc_main.cpp", "w") as f:
        f.write(main_content)


# --- Pipeline entry point ---


def main():
    import argparse
    import subprocess
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdfg", type=str, default="cloudsc_pydace_simplified.sdfgz")
    parser.add_argument("--release", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    sdfg = dace.SDFG.from_file(args.sdfg)
    sdfg.name = "cloudsc_py"

    # Set build folder explicitly (like velocity)
    codegen_dir = Path("codegen")
    if codegen_dir.exists():
        shutil.rmtree(codegen_dir)
    codegen_dir.mkdir()
    sdfg.build_folder = str(codegen_dir / sdfg.name)

    print(f"Generating code ({'Release' if args.release else 'Debug'})...")

    # Use velocity's lower-level codegen path
    sdfg.fill_scope_connectors()
    infer_types.infer_connector_types(sdfg)
    infer_types.set_default_schedule_and_storage_types(sdfg, None)
    sdfg.expand_library_nodes()
    infer_types.infer_connector_types(sdfg)
    infer_types.set_default_schedule_and_storage_types(sdfg, None)

    program_objects = codegen.generate_code(sdfg, validate=False)
    compiler.generate_program_folder(sdfg, program_objects, sdfg.build_folder)

    build_loc = Path(sdfg.build_folder)
    modify_files_in_directory(build_loc)

    # Flatten: move files up to codegen/ and clean up build subfolder
    _, header = flatten_build_folder(build_loc, sdfg.name)

    generate_main_cpp(Path(header), sdfg)

    dace_runtime = Path(dace.__file__).parent / "runtime" / "include"

    # Detect HDF5
    h5_cflags = ""
    h5_libs = ""
    try:
        h5_cflags = subprocess.check_output(["pkg-config", "--cflags", "hdf5"], text=True).strip()
        h5_libs = subprocess.check_output(["pkg-config", "--libs", "hdf5"], text=True).strip()
    except Exception:
        try:
            h5_prefix = subprocess.check_output(["brew", "--prefix", "hdf5"], text=True).strip()
            h5_cflags = f"-I{h5_prefix}/include"
            h5_libs = f"-L{h5_prefix}/lib -lhdf5"
        except Exception:
            pass

    if args.release:
        cpp_flags = "-O3 -std=c++20 -DNDEBUG"
    else:
        cpp_flags = "-O0 -g -std=c++20 -Wall -Wextra"

    cmd = f"c++ {cpp_flags} -Icodegen -I{dace_runtime} {h5_cflags} cloudsc_main.cpp codegen/*.cpp -o cloudsc_cpu_bin -lpthread {h5_libs}"

    with open("recompile.sh", "w") as f:
        f.write(f"#!/bin/bash\nset -e\n{cmd}\n")
    os.chmod("recompile.sh", 0o755)
    print(f"Pipeline ready. Build command updated in recompile.sh (Release={args.release}, HDF5 support included)")


if __name__ == "__main__":
    main()
