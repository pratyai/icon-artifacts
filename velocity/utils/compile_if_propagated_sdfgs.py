import dace
import os
import shutil
from pathlib import Path
import typing
import re

from dace.sdfg import infer_types
from utils.config import fix_out_val_0, rm_syncs
from utils.prune_names import prune_names, compare_structs

use_cuda_events = os.getenv("_USE_CUDA_EVENTS", "0").lower() in ("1", "true", "yes")


# --- Source Management ---


def _replace_cpp_with_cu(directory):
    directory = Path(directory)
    for ext in ["*.cpp", "*.cc"]:
        for file in directory.rglob(ext):
            new_name = file.with_suffix(".cu")
            file.rename(new_name)
            print(f"Renamed: {file} -> {new_name}")


def flatten_build_folder(build_loc: Path, sdfg_name: str, gpu: bool):
    """
    Moves necessary files to the parent of build_loc and removes build_loc.
    """
    cpu_ext = "cu" if gpu else "cpp"
    # The files might have been renamed by _replace_cpp_with_cu or exist as .cpp/.cc
    cpu_path = None
    for ext in ["cu", "cpp", "cc"]:
        p = build_loc / "src" / "cpu" / f"{sdfg_name}.{ext}"
        if p.exists():
            cpu_path = p
            cpu_ext = ext
            break

    dev_path = build_loc / "src" / "cuda" / f"{sdfg_name}_cuda.cu"
    header_path = build_loc / "include" / f"{sdfg_name}.h"

    target_cpu = build_loc.parent / f"{sdfg_name}.{cpu_ext}"
    target_dev = build_loc.parent / f"{sdfg_name}_cuda.cu"
    target_header = build_loc.parent / f"{sdfg_name}.h"

    if cpu_path and cpu_path.exists():
        shutil.move(str(cpu_path), str(target_cpu))
    if gpu and dev_path.exists():
        shutil.move(str(dev_path), str(target_dev))
    if header_path.exists():
        shutil.move(str(header_path), str(target_header))

    # Clean up the original build folder
    shutil.rmtree(build_loc, ignore_errors=True)

    # Patch includes
    files_to_patch = [target_cpu]
    if gpu:
        files_to_patch.append(target_dev)

    for f in files_to_patch:
        if f.exists():
            repl_in_file(str(f), "../../include/hash.h", "hash.h")
            repl_in_file(str(f), f"../../include/{sdfg_name}.h", f"{sdfg_name}.h")

    return (
        str(target_cpu),
        (str(target_dev) if gpu and target_dev.exists() else None),
        str(target_header),
    )


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
        # Only inject static for global definitions, not function parameters or signatures
        if pattern.match(line) and "(" not in line and "," not in line and ";" in line:
            line = pattern.sub(r"\1static int tmp_struct_symbol", line)
            modified = True

        # Similar restriction for __f2dace_ variables to avoid multiple definition linker errors
        if "__f2dace_" in line and "(" not in line and "," not in line and ";" in line:
            line = re.sub(
                r"\bint\s+(__(f2dace_[a-zA-Z0-9_]+));", r"static int \1;", line
            )
            modified = True

        new_lines.append(line)

    if modified:
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)


# --- Basic String Helpers ---


def repl_in_file(file_path: str, src: str, dst: str):
    with open(file_path, "r") as f:
        code = f.read()
    with open(file_path, "w") as f:
        f.write(code.replace(src, dst))


def repl_in_file_per_line_with_cond(
    file_path: str, src: str, dst: str, condition: callable
):
    with open(file_path, "r") as f:
        lines = f.readlines()
    with open(file_path, "w") as f:
        f.writelines(
            [line.replace(src, dst) if condition(line) else line for line in lines]
        )


def set_default_stream(file_path: str):
    repl_in_file(file_path, "__state->gpu_context->streams[0]", "nullptr")


# --- Complex Code Patching ---


def add_timers(file_path: str, gpu: bool, stage: int, use_openacc_stream: bool = False):
    with open(file_path, "r") as f:
        code = f.read()

    if stage < 7:
        pattern1 = r"^\s*nrdmax_jg\s*=\s*__CG_global_data__m_nrdmax\[0\];\s*$"
    else:
        pattern1 = r"^\s*nflatlev_jg\s*=\s*__CG_global_data__m_nflatlev\[0\];\s*$"

    if not gpu:
        replacement1 = ' measure_time("Run"); \n\\g<0>'
    else:
        if 5 < stage < 9:
            if use_cuda_events:
                replacement1 = '   cudaStreamSynchronize(__state->gpu_context->streams[0]); //EntryStreamSync\n      cudaEvent_t start1, stop1;\n    cudaEventCreate(&start1);\n    cudaEventCreate(&stop1);\n    cudaEventRecord(start1); \n //measure_time("Run");\n \\g<0>'
            else:
                replacement1 = '   cudaStreamSynchronize(__state->gpu_context->streams[0]); //EntryStreamSync\n      //cudaEvent_t start1, stop1;\n    //cudaEventCreate(&start1);\n    //cudaEventCreate(&stop1);\n    //cudaEventRecord(start1); \n measure_time("Run");\n \\g<0>'
        elif stage == 9:
            replacement1 = (
                "\\g<0>"
                if use_openacc_stream
                else '   cudaDeviceSynchronize(); //EntryStreamSync\n      //cudaEvent_t start1, stop1;\n    //cudaEventCreate(&start1);\n    //cudaEventCreate(&stop1);\n    //cudaEventRecord(start1); \n measure_time("Run");\n \\g<0>'
            )
        else:
            prefix = "   cudaDeviceSynchronize(); //EntryStreamSync\n      "
            if use_cuda_events:
                replacement1 = f'{prefix}cudaEvent_t start1, stop1;\n    cudaEventCreate(&start1);\n    cudaEventCreate(&stop1);\n    cudaEventRecord(start1); \n //measure_time("Run");\n \\g<0>'
            else:
                replacement1 = f'{prefix}//cudaEvent_t start1, stop1;\n    //cudaEventCreate(&start1);\n    //cudaEventCreate(&stop1);\n    //cudaEventRecord(start1); \n measure_time("Run");\n \\g<0>'

    pattern2 = r"^\s*double p_diag_out_max_vcfl_dyn;\s*$"
    if gpu:
        if 5 < stage < 9:
            if not use_cuda_events:
                replacement2 = '\\g<0>  //cudaEventRecord(stop1);\n    //cudaEventSynchronize(stop1);\n    //float milliseconds1 = 0;\n    //cudaEventElapsedTime(&milliseconds1, start1, stop1);\n     measure_time("Host Based C++ Timer"); \n  //cudaEventDestroy(start1);\n    //cudaEventDestroy(stop1);\n    //cudaStreamSynchronize(__state->gpu_context->streams[0]); \n  //std::cout << "CUDA Events Based Total time: " << milliseconds1*1000.0 << " us" << std::endl;\n'
            else:
                replacement2 = '\\g<0>  cudaEventRecord(stop1);\n    cudaEventSynchronize(stop1);\n    float milliseconds1 = 0;\n    cudaEventElapsedTime(&milliseconds1, start1, stop1);\n     //measure_time("Host Based C++ Timer"); \n  cudaEventDestroy(start1);\n    //cudaEventDestroy(stop1);\n    cudaStreamSynchronize(__state->gpu_context->streams[0]); \n  std::cout << "CUDA Events Based Total time: " << milliseconds1*1000.0 << " us" << std::endl;\n'
        elif stage == 9:
            sync = "cudaStreamSynchronize(__state->gpu_context->streams[0]);"
            final_sync = (
                "//cudaDeviceSynchronize();"
                if use_openacc_stream
                else "cudaDeviceSynchronize();"
            )
            replacement2 = f'\\g<0>  {sync}\n    //cudaEventRecord(stop1);\n    //cudaEventSynchronize(stop1);\n    //float milliseconds1 = 0;\n    //cudaEventElapsedTime(&milliseconds1, start1, stop1);\n     measure_time("Host Based C++ Timer"); \n  //cudaEventDestroy(start1);\n    //cudaEventDestroy(stop1);\n    {final_sync} \n  //std::cout << "CUDA Events Based Total time: " << milliseconds1*1000.0 << " us" << std::endl;\n'
        else:
            if use_cuda_events:
                replacement2 = '\\g<0>  cudaEventRecord(stop1);\n    cudaEventSynchronize(stop1);\n    float milliseconds1 = 0;\n    //cudaEventElapsedTime(&milliseconds1, start1, stop1);\n     //measure_time("Host Based C++ Timer"); \n  cudaEventDestroy(start1);\n    //cudaEventDestroy(stop1);\n    cudaDeviceSynchronize(); \n  std::cout << "CUDA Events Based Total time: " << milliseconds1*1000.0 << " us" << std::endl;\n'
            else:
                replacement2 = '\\g<0>  //cudaEventRecord(stop1);\n    //cudaEventSynchronize(stop1);\n    //float milliseconds1 = 0;\n    //cudaEventElapsedTime(&milliseconds1, start1, stop1);\n     measure_time("Host Based C++ Timer"); \n  //cudaEventDestroy(start1);\n    //cudaEventDestroy(stop1);\n    cudaDeviceSynchronize(); \n  //std::cout << "CUDA Events Based Total time: " << milliseconds1*1000.0 << " us" << std::endl;\n'
    else:
        replacement2 = '\\g<0>  measure_time("Run");\n'

    code = re.sub(pattern1, replacement1, code, flags=re.MULTILINE)
    code = re.sub(pattern2, replacement2, code, flags=re.MULTILINE)

    if gpu and stage > 5:
        pattern4 = """dace::CopyNDDynamic<double, 1, false, 1>::template ConstDst<1>::Copy(
            __state->__0_gpu_vcflmax, __state->__0_vcflmax, tmp_struct_symbol_12, 1);"""
        replacement4 = "DACE_GPU_CHECK(cudaMemcpyAsync((void*)__state->__0_vcflmax, (void*)__state->__0_gpu_vcflmax, static_cast<size_t>(tmp_struct_symbol_12) * sizeof(double), cudaMemcpyDeviceToHost, __state->gpu_context->streams[0]));"
        code = code.replace(pattern4, replacement4)
        pattern5 = r"(^\s*double\s*.*\s*in_arr\s*=.*vcflmax.*;)"
        replacement5 = r"cudaStreamSynchronize(__state->gpu_context->streams[0]);//ExitStreamSync\n\1"
        code = re.sub(pattern5, replacement5, code, flags=re.MULTILINE)

    with open(file_path, "w") as f:
        f.write(code)


def change_to_openacc_stream(host_file_path: str, dev_file_path: str, gpu: bool):
    assert gpu is True
    stream_decl = """
// This part is generated by the IconGrounds velocity utils.
#include <cuda_runtime.h>
#include <openacc.h>
static cudaStream_t open_acc_stream;
"""
    host_check = "void __program_velocity_no_nproma_if_prop"
    dev_check = "DACE_EXPORTED int __dace_init_cuda_"
    for file_path, check in [(host_file_path, host_check), (dev_file_path, dev_check)]:
        with open(file_path, "r") as f:
            lines = f.readlines()
        modified = [stream_decl]
        i = 0
        while i < len(lines):
            line = lines[i]
            if "__dace_current_stream = __state->gpu_context->streams" in line:
                modified.append(
                    "cudaStream_t __dace_current_stream = open_acc_stream;\n"
                )
            elif (
                any(
                    x in line
                    for x in [
                        "cudaStreamCreate",
                        "cudaStreamDestroy",
                        "cudaEventCreate",
                        "cudaEventDestroy",
                        "internal_streams[",
                    ]
                )
                and "cudaLaunchKernel" not in line
            ):
                modified.append("//" + line)
            elif "__state->gpu_context->streams[0]" in line:
                modified.append(
                    line.replace("__state->gpu_context->streams[0]", "open_acc_stream")
                )
            elif check in line:
                modified.append(line)
                if (
                    check == host_check
                    and "_internal" in line
                    and "{" not in line
                    and ";" not in line
                    and "DACE_EXPORTED" not in line
                ):
                    i += 1
                    if i < len(lines) and lines[i].strip() == "{":
                        modified.append(
                            "{\nopen_acc_stream = (cudaStream_t) acc_get_cuda_stream(1);\ncudaStreamSynchronize(open_acc_stream); //EntryStreamSync\n"
                        )
                        if use_cuda_events:
                            modified.append(
                                "    cudaEvent_t start1, stop1;\n    cudaEventCreate(&start1);\n    cudaEventCreate(&stop1);\n    cudaEventRecord(start1); \n"
                            )
                        else:
                            modified.append('measure_time("Run");\n')
                elif check == dev_check and "{" in line:
                    modified.append(
                        "open_acc_stream = (cudaStream_t) acc_get_cuda_stream(1);\n"
                    )
            else:
                modified.append(line)
            i += 1
        with open(file_path, "w") as f:
            f.writelines(modified)


def fix_levelmask_calls(filepath: str, host: bool, stage: int):
    with open(filepath, "r") as file:
        lines = file.readlines()
    with open(filepath, "w") as file:
        for line in lines:
            if stage <= 5:
                p1, r1 = (
                    "uint8_t  gpu_levelmask, double *",
                    "uint8_t* __restrict__  gpu_levelmask, double *",
                )
                p2, r2 = (
                    "gpu_levelmask, &",
                    ("&gpu_levelmask[0], &" if host else "gpu_levelmask[0], &"),
                )
            else:
                p1, r1 = (
                    (
                        "uint8_t  gpu_levelmask, double *"
                        if host
                        else "uint8_t gpu_levelmask, double *"
                    ),
                    "uint8_t* __restrict__  gpu_levelmask, double *",
                )
                p2, r2 = (
                    (
                        "__state->__0_gpu_levelmask, &"
                        if host
                        else "&__state->__0_gpu_levelmask, &"
                    ),
                    "&__state->__0_gpu_levelmask[0], &",
                )
            line = line.replace(p1, r1).replace(p2, r2)
            if not host:
                line = line.replace(
                    "uint8_t in_arr = gpu_cfl_clipping;",
                    "uint8_t in_arr = gpu_cfl_clipping[((_for_it_22 + (tmp_struct_symbol_14 * (_for_it_35 - 1))) - 1)];",
                )
                line = line.replace(
                    "uint8_t  gpu_cfl_clipping,",
                    "uint8_t* __restrict__  gpu_cfl_clipping,",
                )
            file.write(line.replace(", &gpu_levelmask,", ", &gpu_levelmask[0],"))


def add_reduce_clean_up_calls(filepath: str):
    pattern = "DACE_EXPORTED int __dace_exit_velocity_no_nproma_if_prop"
    with open(filepath, "r") as file:
        lines = file.readlines()
    new_lines = []
    i = 0
    while i < len(lines):
        new_lines.append(lines[i])
        if pattern in lines[i]:
            while i < len(lines) and "{" not in lines[i]:
                i += 1
                new_lines.append(lines[i])
            if i < len(lines):
                indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                new_lines.append(
                    f"{indent}  cleanup_reduce_sum_gpu();\n{indent}  cleanup_reduce_maxZ_gpu();\n"
                )
        i += 1
    with open(filepath, "w") as file:
        file.writelines(new_lines)


def comment_out_syncs(filepath: str, gpu: bool):
    with open(filepath, "r") as file:
        lines = file.readlines()
    with open(filepath, "w") as file:
        for line in lines:
            if any(
                x in line
                for x in ["cudaStreamSynchronize", "EventRecord", "StreamWaitEvent"]
            ):
                if not any(x in line for x in ["stop", "start"]):
                    line = "//" + line
            if "tmp_call_18 = -1.7976931348623157e+308;" in line:
                line = (
                    (
                        "DACE_GPU_CHECK(cudaStreamSynchronize(__state->gpu_context->streams[0]));\n"
                        + line
                    )
                    if gpu
                    else "//" + line
                )
            file.write(line)


def _name_match(name: str, line: str) -> bool:
    """Check that *name* appears in *line* as a whole identifier, not as a
    substring of a longer name (e.g. ``edge_blk`` must not match
    ``edge_blk_uint8``).  The character immediately after the match must be
    a non-identifier character (not alphanumeric / underscore)."""
    start = 0
    while True:
        idx = line.find(name, start)
        if idx == -1:
            return False
        end = idx + len(name)
        if end >= len(line) or not (line[end].isalnum() or line[end] == '_'):
            return True
        start = end


def comment_out_allocs_and_frees(filepath: str, name_set: typing.Set[str]):
    with open(filepath, "r") as file:
        lines = file.readlines()
    with open(filepath, "w") as file:
        commenting_block = False  # True while commenting a multi-line statement
        for i, line in enumerate(lines):
            stripped = line.strip()

            # Continue commenting until we hit the closing semicolon
            if commenting_block:
                indent = line[: len(line) - len(line.lstrip())]
                file.write(indent + "//" + line.lstrip())
                if ";" in stripped:
                    commenting_block = False
                continue

            if stripped.startswith("//"):
                file.write(line)
                continue

            should_comment = False
            for name in name_set:
                # delete/free on same line
                if (
                    any(
                        x in line
                        for x in ["delete[]", "delete", "cudaFree", "cudaMalloc"]
                    )
                    and _name_match(name, line)
                ):
                    # Don't comment out per-call boundary-cast buffer allocations.
                    # These are local float* variables in __program_internal
                    # (no "__state->" prefix), needed for fp64→float32 casting.
                    if ("cudaMalloc" in line or "cudaFree" in line) and "__state->" not in line:
                        continue
                    should_comment = True
                    break
                # Single-line: name = new DACE_ALIGN(...);
                if (
                    _name_match(name, line)
                    and "=" in line
                    and "new" in line
                    and "DACE_ALIGN" in line
                ):
                    should_comment = True
                    break
                # Multi-line: name =\n    new DACE_ALIGN(...)...;
                # Detect "name =" without "new" on this line, but "new" on next line
                if (
                    _name_match(name, line)
                    and "=" in line
                    and "new" not in line
                    and i + 1 < len(lines)
                    and "new" in lines[i + 1]
                    and "DACE_ALIGN" in lines[i + 1]
                ):
                    should_comment = True
                    break
            if should_comment:
                indent = line[: len(line) - len(line.lstrip())]
                file.write(indent + "//" + line.lstrip())
                # If no semicolon on this line, keep commenting continuation lines
                if ";" not in stripped:
                    commenting_block = True
            else:
                file.write(line)


def use_solve_nh_struct_definitions(filepath: str):
    struct_names = {
        "global_data_type",
        "t_tangent_vectors",
        "t_grid_edges",
        "t_nh_metrics",
        "t_prepare_adv",
        "t_nh_ref",
        "t_nh_prog",
        "t_nh_diag",
        "t_nh_state",
        "t_grid_domain_decomp_info",
        "t_grid_cells",
        "t_int_state",
        "t_grid_vertices",
        "t_patch",
    }
    with open(filepath, "r") as f:
        lines = f.read().split("\n")
    filtered = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if (
            re.match(r"\s*struct\s+(\w+)\s*;", line)
            and re.match(r"\s*struct\s+(\w+)\s*;", line).group(1) in struct_names
        ):
            i += 1
            continue
        if (
            re.search(r"\bstruct\s+(\w+)\s*\{", line)
            and re.search(r"\bstruct\s+(\w+)\s*\{", line).group(1) in struct_names
        ):
            i += 1
            while i < len(lines) and lines[i].strip() != "};":
                i += 1
            i += 1
            continue
        filtered.append(line)
        i += 1
    with open(filepath, "w") as f:
        f.write('#include "shared_struct_defs.h"\n' + "\n".join(filtered))


def replace_pass_by_copy_to_pass_by_ref(path: str):
    with open(path, "r") as f:
        content = f.read()
    content = re.sub(
        r" double __CG_p_diag__m_max_vcfl_dyn,",
        r" double& __CG_p_diag__m_max_vcfl_dyn,",
        content,
    )
    content = re.sub(
        r" double __CG_p_diag__m_max_vcfl_dyn[)]",
        r" double& __CG_p_diag__m_max_vcfl_dyn)",
        content,
    )
    with open(path, "w") as f:
        f.write(content)


def fix_mixed_precision_ambiguity(file_path: Path):
    """Inject fp16_operators.h to resolve double/half operator ambiguity."""
    with open(file_path, "r") as f:
        content = f.read()

    include = '#include "fp16_operators.h"'
    if include in content:
        return

    # Insert after the first #include line
    new_content = re.sub(
        r"(#include\s+[<\"][^>\"]+[>\"])",
        rf"\1\n{include}",
        content,
        count=1,
    )

    if new_content != content:
        with open(file_path, "w") as f:
            f.write(new_content)


def patch_bfp_reads(
    code: str, bfp_gpu_names: list[str], block_size: int = 32, mantissa_bits: int = 16
) -> str:
    """Text-level BFP patching: fix parameter types and replace array reads
    with bfp_decode calls for GPU arrays that were BFP-packed at the top level
    but whose nested SDFG descriptors still say double*.

    Patches:
      - `(const) double *(__restrict__) gpu_NAME` → `const uint8_t *__restrict__ gpu_NAME`
      - `gpu_NAME[(index)]` → `bfp_decode<BS, MB>(gpu_NAME, (int)(index))`
    """
    for gpu_name in bfp_gpu_names:
        # 1. Fix parameter / declaration types (more robust regex for double*)
        # Matches: double* gpu_NAME, const double * __restrict__ gpu_NAME, etc.
        code = re.sub(
            rf"(const\s+)?double\s*\*\s*(__restrict__\s+)?{re.escape(gpu_name)}\b",
            f"const uint8_t *__restrict__ {gpu_name}",
            code,
        )

        # 1b. Fix pointer casts: (double *)(&gpu_NAME[...]) → &gpu_NAME[...]
        code = re.sub(
            rf"\(double\s*\*\)\s*\(\s*&{re.escape(gpu_name)}\b",
            f"(const uint8_t *)(&{gpu_name}",
            code,
        )

        # 2. Replace array reads: gpu_NAME[(expr)] → bfp_decode(...)
        #    Skip pointer passes like &gpu_NAME[0] — these pass the raw
        #    pointer to kernel launch args, not element reads.
        result = []
        i = 0
        search = f"{gpu_name}["
        while i < len(code):
            pos = code.find(search, i)
            if pos == -1:
                result.append(code[i:])
                break
            result.append(code[i:pos])
            # Find matching ] by counting brackets
            bracket_start = pos + len(search) - 1  # position of [
            depth = 1
            j = bracket_start + 1
            while j < len(code) and depth > 0:
                if code[j] == "[":
                    depth += 1
                elif code[j] == "]":
                    depth -= 1
                j += 1
            if depth != 0:
                # Unmatched bracket — leave as-is
                result.append(code[pos : pos + len(search)])
                i = pos + len(search)
                continue
            index_expr = code[bracket_start + 1 : j - 1].strip()
            # Check if preceded by & (address-of → pointer pass, not read)
            text_before = code[:pos].rstrip()
            if text_before.endswith("&"):
                # Pointer pass: &gpu_NAME[expr] — leave as-is
                result.append(code[pos:j])
            else:
                result.append(
                    f"bfp_decode<{block_size}, {mantissa_bits}>({gpu_name}, (int)({index_expr}))"
                )
            i = j
        code = "".join(result)

    return code


def patch_block_loop_bounds(code: str) -> str:
    """Clamp start_block and start_index reads to max(1, ...).

    ICON uses 0-based sentinel values in start_block/start_index arrays to
    encode empty refinement-level ranges.  Fortran's get_indices_v makes the
    inner loop empty for these blocks, but DaCe's generated CUDA code launches
    kernels covering the full block range, causing negative array offsets and
    CUDA_ERROR_ILLEGAL_ADDRESS.

    This patch wraps every assignment that reads from a start_block or
    start_index array in std::max(1, ...), so sentinel values produce empty
    loop ranges [1, 0] instead of invalid accesses.

    Pattern matched (assignment context only):
        VAR = __CG_...__m_start_block[EXPR];
        VAR = __CG_...__m_start_index[EXPR];
    Becomes:
        VAR = std::max(1, __CG_...__m_start_block[EXPR]);
        VAR = std::max(1, __CG_...__m_start_index[EXPR]);
    """
    # Clamp start_block reads: ICON may return 0 (sentinel for empty
    # refinement level), but data arrays have LBOUND=1 in block dim.
    # Note: codegen may split the expression across lines, so we use
    # [\s\S] instead of [^\]] to match newlines inside brackets.
    code = re.sub(
        r"(=\s*)(__CG_\w+__m_start_block\[[\s\S]*?\])\s*;",
        r"\1std::max(1, \2); // patched: clamp block sentinel",
        code,
    )
    # Clamp start_index reads: same issue in nproma dim.
    # Matches get_indices_c_lib's MAX(1, i_startidx_in) which
    # get_indices_v_lib lacks.
    code = re.sub(
        r"(=\s*)(__CG_\w+__m_start_index\[[\s\S]*?\])\s*;",
        r"\1std::max(1, \2); // patched: clamp index sentinel",
        code,
    )
    return code


def _add_bfp_include(file_path: Path):
    """Inject bfp.cuh include for BFP decode support in generated CUDA code."""
    with open(file_path, "r") as f:
        content = f.read()

    include = '#include "bfp.cuh"'
    if include in content:
        return

    # Insert after the first #include line
    new_content = re.sub(
        r"(#include\s+[<\"][^>\"]+[>\"])",
        rf"\1\n{include}",
        content,
        count=1,
    )

    if new_content != content:
        with open(file_path, "w") as f:
            f.write(new_content)


# --- Init CUDA signature fix (extracted to utils/fix_init_cuda_odr.py) ---
from utils.fix_init_cuda_odr import fix_init_cuda_0_signature


# --- Compilation Flow ---


def compile_if_propagated_sdfgs(
    sdfgs,
    gpu,
    release,
    generate_code,
    lib,
    main_name,
    stage,
    debuginfo,
    allocation_names_to_comment_out,
    use_openacc_stream,
    output_name=None,
):
    from utils.generate_storage_types import (
        generate_velocity_tendencies_h,
        patch_shared_struct_defs_h,
    )

    patch_shared_struct_defs_h(sdfgs=sdfgs)

    if os.getenv("SINGLE_THREADED", "0").lower() in ("1", "true", "yes"):
        dace.config.Config.set("compiler", "num_threads", value="1")

    compare_structs(sdfgs)
    sources = {
        "src/reductions.cpp",
        "src/timer.cpp",
        "src/sqlite_logger.cpp",
        "src/gpu_mem.cpp",
    }
    if gpu:
        sources.add("src/reductions_kernel.cu")
    from dace.codegen import codegen, compiler

    for sdfg in sdfgs:
        build_loc = Path(sdfg.build_folder)
        if generate_code:
            sdfg.fill_scope_connectors()
            infer_types.infer_connector_types(sdfg)
            infer_types.set_default_schedule_and_storage_types(sdfg, None)
            sdfg.expand_library_nodes()
            infer_types.infer_connector_types(sdfg)
            infer_types.set_default_schedule_and_storage_types(sdfg, None)
            sdfg.save(sdfg.name + "_concretized.sdfgz", compress=True)

            if os.getenv("_PERMUTE_DIMENSIONS", "0").lower() in ("1", "true", "yes"):
                from utils.reshape_kernels import update_gpu_block_size

                update_gpu_block_size(sdfg, [32, 32, 1])

            program_objects = codegen.generate_code(sdfg, validate=False)
            compiler.generate_program_folder(sdfg, program_objects, sdfg.build_folder)
            modify_files_in_directory(build_loc)

            if os.getenv("_LOWPREC", "fp64").lower() in (
                "fp16",
                "f16",
                "bfp8",
                "bfp16",
            ):
                for cu_file in build_loc.rglob("*.cu"):
                    fix_mixed_precision_ambiguity(cu_file)

            # BFP decode shims in generated CUDA need bfp.cuh.
            # Always inject — harmless no-op if no BFP arrays are present.
            for cu_file in build_loc.rglob("*.cu"):
                _add_bfp_include(cu_file)

            # Text-level BFP patching: fix parameter types and replace
            # double reads with bfp_decode() calls in generated kernels.
            # The SDFG-level BFP transform only changes the top-level
            # allocation; nested SDFGs (GPU kernels) still emit double*.
            _lowprec = os.getenv("_LOWPREC", "fp64").lower()
            if _lowprec.startswith("bfp"):
                # Precision Autodiscovery: check descriptors for the BFP_PACKED tag
                _bfp_gpu_names = [
                    n
                    for n, arr in sdfg.arrays.items()
                    if arr.debuginfo and arr.debuginfo.filename == "BFP_PACKED"
                ]

                if _bfp_gpu_names:
                    print(
                        f"BFP: Autodiscovered {len(_bfp_gpu_names)} arrays for patching: {_bfp_gpu_names}"
                    )

                _bfp_mbits = {"bfp8": 8, "bfp16": 16, "bfp32": 16}.get(_lowprec, 16)

                for src_file in (
                    list(build_loc.rglob("*.cu"))
                    + list(build_loc.rglob("*.cpp"))
                    + list(build_loc.rglob("*.h"))
                ):
                    with open(src_file, "r") as f:
                        content = f.read()

                    patched = patch_bfp_reads(
                        content, _bfp_gpu_names, mantissa_bits=_bfp_mbits
                    )

                    if patched != content:
                        print(f"BFP: Patched {src_file}")
                        with open(src_file, "w") as f:
                            f.write(patched)

        cpu_src = f"{build_loc}/src/cpu/{sdfg.name}.{'cu' if gpu else 'cpp'}"
        dev_src = f"{build_loc}/src/cuda/{sdfg.name}_cuda.cu"
        header = f"{build_loc}/include/{sdfg.name}.h"

        if gpu:
            _replace_cpp_with_cu(build_loc)

            # Clamp start_block/start_index reads to max(1, ...) so that
            # ICON's 0-valued sentinels produce empty loop ranges instead
            # of negative array offsets on GPU.  Must run after
            # _replace_cpp_with_cu so the CPU file is .cu.
            # DISABLED: clamp patch was based on wrong diagnosis.
            # The Fortran values are valid (>=1); the issue is in ctor/SOA.
            # _n_patched = 0
            # for cu_file in build_loc.rglob("*.cu"):
            #     with open(cu_file, "r") as f:
            #         content = f.read()
            #     patched = patch_block_loop_bounds(content)
            #     if patched != content:
            #         _n_patched += 1
            #         with open(cu_file, "w") as f:
            #             f.write(patched)
            # if _n_patched:
            #     print(f"Block bounds: Patched {_n_patched} files")
            # else:
            #     print("Block bounds: WARNING — no start_block/start_index reads found to patch")

            if stage > 5 and rm_syncs:
                comment_out_syncs(cpu_src, gpu)
            if allocation_names_to_comment_out and stage >= 8:
                comment_out_allocs_and_frees(cpu_src, allocation_names_to_comment_out)
            if use_openacc_stream and stage >= 8:
                change_to_openacc_stream(cpu_src, dev_src, gpu)
            if stage >= 8:
                fix_init_cuda_0_signature(cpu_src, dev_src)
            add_reduce_clean_up_calls(cpu_src)
            fix_levelmask_calls(cpu_src, True, stage)
            if stage > 5:
                with open(cpu_src, "r") as f:
                    code = f.read()
                # Zero maxvcfl_arr at the start of each __program call.
                # It's only written for cells where cfl_clipping is true,
                # so stale values from prior calls would contaminate the
                # per-block MAX reduction.
                # Extract the size expression from the existing cudaMemset in __dace_init.
                m = re.search(
                    r"cudaMemset\(__state->__0_gpu_maxvcfl_arr, 0,\s*\n?\s*(.*?)\s*\*\s*\n?\s*sizeof\(double\)\)",
                    code,
                    re.DOTALL,
                )
                if m:
                    size_expr = " ".join(m.group(1).split())
                    code = code.replace(
                        "nflatlev_jg = __CG_global_data__m_nflatlev[0];",
                        f"nflatlev_jg = __CG_global_data__m_nflatlev[0];\n"
                        f"  DACE_GPU_CHECK(cudaMemset(__state->__0_gpu_maxvcfl_arr, 0, {size_expr} * sizeof(double)));",
                    )
                # CFL patches — commented out to isolate kernel crash
                # Zero-initialize vcflmax (GPU and CPU) — blocks outside the
                # per-block loop range are never written, so the final reduction
                # would read uninitialized memory.
                code = re.sub(
                    r"(DACE_GPU_CHECK\(cudaMalloc\(\(void \*\*\)&__state->__0_gpu_vcflmax,\s*\n?\s*(\w+) \* sizeof\(double\)\)\);)",
                    r"\1\n  DACE_GPU_CHECK(cudaMemset(__state->__0_gpu_vcflmax, 0, \2 * sizeof(double)));",
                    code,
                )
                code = re.sub(
                    r"(__state->__0_vcflmax = new double DACE_ALIGN\(64\)\[(\w+)\];)",
                    r"\1\n  memset(__state->__0_vcflmax, 0, \2 * sizeof(double));",
                    code,
                )
                # Fix max_vcfl_dyn: the SDFG lost the MAX(old, new) — it just
                # overwrites with the reduction result. Restore the MAX.
                code = code.replace(
                    "p_diag_out_max_vcfl_dyn = max_vcfl_dyn_var_152_0_in;",
                    "p_diag_out_max_vcfl_dyn = (max_vcfl_dyn_var_152_0_in > __CG_p_diag__m_max_vcfl_dyn) ? max_vcfl_dyn_var_152_0_in : __CG_p_diag__m_max_vcfl_dyn;",
                )
                # Fix scalar reduce: the SDFG doesn't wrap this block with
                # #define __REDUCE_GPU__, so it falls through to _cpu —
                # but the data lives on GPU. Replace the #ifdef block with
                # a direct GPU call and point at the GPU buffer.
                code = code.replace(
                    "&__state->__0_vcflmax[",
                    "&__state->__0_gpu_vcflmax[",
                )
                code = re.sub(
                    r"#ifdef __REDUCE_DEVICE__\s*\n\s*out = reduce_maxZ_to_scalar_device\(in_arr, in_size\);\s*\n"
                    r"\s*#elif defined\(__REDUCE_GPU__\)\s*\n"
                    r"\s*out = reduce_maxZ_to_scalar_gpu\(in_arr, in_size, __dace_current_stream\);\s*\n"
                    r"\s*#else\s*\n"
                    r"\s*out = reduce_maxZ_to_scalar_cpu\(in_arr, in_size\);\s*\n"
                    r"\s*#endif",
                    "out = reduce_maxZ_to_scalar_gpu(in_arr, in_size,\n"
                    "          __state->gpu_context->streams[0]);",
                    code,
                )
                # Floatify bare double literals in branch conditions (interstate
                # edges).  The SDFG-level floatify only touches tasklet source,
                # but DaCe codegen emits double literals for interstate edge
                # conditions like `vcfl < -0.85`.
                # Match literals NOT already suffixed with 'f' or wrapped in float().
                for _val in ["0.85", "0.5", "1.0", "0.0", "0.05", "0.65", "1.15"]:
                    code = re.sub(
                        rf"(?<!float\()(?<![0-9]){re.escape(_val)}(?!f)(?![0-9])",
                        f"{_val}f",
                        code,
                    )

                # Safety net: replace any remaining dace::math::ipow(x, 2) with (x)*(x)
                # These come from Python tasklets where ** 2 is converted by DaCe codegen.
                code = re.sub(
                    r"dace::math::ipow\(([^,]+),\s*2\)",
                    r"((\1) * (\1))",
                    code,
                )
                # Fix ambiguous overload: CFL tasklets mix fp16 scalars with
                # float/double literals.  Wrap fp16 vars in static_cast<float>
                # to avoid device-only __half operators.
                # Patterns may have raw literals, f-suffixed, or float()-wrapped
                # depending on whether floatify ran on CPP or Python tasklets.
                _F = r"(?:float\()?0\.65f?\)?"  # matches 0.65, 0.65f, float(0.65)
                code = re.sub(
                    rf"\({_F} / dtime_0_in\)",
                    "(0.65f / static_cast<float>(dtime_0_in))",
                    code,
                )
                _F05 = r"(?:float\()?0\.05f?\)?"
                _F85 = r"(?:float\()?0\.85f?\)?"
                code = re.sub(
                    rf"\({_F05} / \(dtime_0_in \* \({_F85} - \(cfl_w_limit_0_in \* dtime_1_in\)\)\)\)",
                    "(0.05f / (static_cast<float>(dtime_0_in) * (0.85f - (static_cast<float>(cfl_w_limit_0_in) * static_cast<float>(dtime_1_in)))))",
                    code,
                )
                # ABI fix: non-transient scalars are lowered internally but
                # Fortran passes double by reference.  Rename the parameter
                # to __abi_X (double) and shadow with a lowered local.
                _ABI_SCALARS = ["dtime", "dt_linintp_ubc"]
                # Detect the lowered type from generated code
                _lowered_ctype = None
                for _lt in ["dace::float16", "float"]:
                    if f"{_lt} {_ABI_SCALARS[0]}," in code:
                        _lowered_ctype = _lt
                        break
                if _lowered_ctype:
                    # Only patch the 3 Fortran-facing functions, not __dace_runkernel_*
                    _ext_fns = [
                        f"__dace_init_{sdfg.name}(",
                        f"__program_{sdfg.name}(",
                        f"__program_{sdfg.name}_internal(",
                    ]
                    for _fn in _ext_fns:
                        _pos = code.find(_fn)
                        while _pos != -1:
                            _sig_end = code.find(")", _pos)
                            if _sig_end == -1:
                                break
                            _sig = code[_pos : _sig_end + 1]
                            # Rename parameters in this signature
                            _new_sig = _sig
                            for _sc in _ABI_SCALARS:
                                _new_sig = _new_sig.replace(
                                    f"{_lowered_ctype} {_sc},", f"double __abi_{_sc},"
                                )
                                _new_sig = _new_sig.replace(
                                    f"{_lowered_ctype} {_sc})", f"double __abi_{_sc})"
                                )
                            code = code[:_pos] + _new_sig + code[_sig_end + 1 :]
                            # Insert shadow locals after opening brace
                            if "__abi_" in _new_sig:
                                _brace = code.find("{", _pos + len(_new_sig))
                                if _brace != -1:
                                    _shadow = "".join(
                                        f"\n  {_lowered_ctype} {_sc} = static_cast<{_lowered_ctype}>(__abi_{_sc});"
                                        for _sc in _ABI_SCALARS
                                        if f"__abi_{_sc}" in _new_sig
                                    )
                                    code = (
                                        code[: _brace + 1]
                                        + _shadow
                                        + code[_brace + 1 :]
                                    )
                            _pos = code.find(_fn, _pos + len(_new_sig) + 1)
                with open(cpu_src, "w") as f:
                    f.write(code)
                if _lowered_ctype:
                    with open(header, "r") as f:
                        hdr = f.read()
                    for _fn in _ext_fns:
                        _pos = hdr.find(_fn)
                        while _pos != -1:
                            _sig_end = hdr.find(";", _pos)
                            if _sig_end == -1:
                                break
                            _sig = hdr[_pos : _sig_end + 1]
                            _new_sig = _sig
                            for _sc in _ABI_SCALARS:
                                _new_sig = _new_sig.replace(
                                    f"{_lowered_ctype} {_sc},", f"double __abi_{_sc},"
                                )
                                _new_sig = _new_sig.replace(
                                    f"{_lowered_ctype} {_sc})", f"double __abi_{_sc})"
                                )
                            hdr = hdr[:_pos] + _new_sig + hdr[_sig_end + 1 :]
                            _pos = hdr.find(_fn, _pos + len(_new_sig) + 1)
                    with open(header, "w") as f:
                        f.write(hdr)
                fix_levelmask_calls(dev_src, False, stage)
                repl_in_file(dev_src, "const const", "const")
                repl_in_file(cpu_src, "const const", "const")
                with open(dev_src, "r") as f:
                    content = f.read()
                with open(dev_src, "w") as f:
                    f.write(
                        '#include "reductions_device.cuh"\n#define __REDUCE_DEVICE__\n'
                        + content
                    )
            if stage == 9:
                for f in [cpu_src, dev_src, header]:
                    repl_in_file_per_line_with_cond(
                        f,
                        "double __CG_p_diag__m_max_vcfl_dyn",
                        "double &__CG_p_diag__m_max_vcfl_dyn",
                        lambda l: "__CG_p_diag__m_max_vcfl_dyn" in l and ";" not in l,
                    )
            with open(cpu_src, "r") as f:
                content = f.read()
            with open(cpu_src, "w") as f:
                f.write(
                    '#include "reductions_kernel.cuh"\n#include "reductions_cpu.h"\n#include "timer.h"\n#include "gpu_mem.h"\n'
                    + content
                )
            if stage in [8, 9]:
                set_default_stream(cpu_src)
                set_default_stream(dev_src)

            # Flatten build folder: move source/header to parent and clean up
            cpu_src, dev_src, header = flatten_build_folder(build_loc, sdfg.name, gpu)

            if dev_src:
                sources.add(dev_src)
            sources.add(cpu_src)
        else:
            with open(cpu_src, "r") as f:
                content = f.read()
            with open(cpu_src, "w") as f:
                f.write(
                    '#include "reductions_cpu.h"\n#include "timer.h"\n#include "gpu_mem.h"\n'
                    + content
                )

            # Flatten build folder: move source/header to root and clean up
            cpu_src, _, header = flatten_build_folder(build_loc, sdfg.name, gpu)

            sources.add(cpu_src)

        if (
            os.getenv("_BUILD_LIB_FOR_SOLVE_NH", "0").lower() in ("1", "true", "yes")
            or stage == 8
        ):
            if stage in [1, 8, 9]:
                use_solve_nh_struct_definitions(cpu_src)
                use_solve_nh_struct_definitions(header)
                if gpu and stage != 1:
                    use_solve_nh_struct_definitions(dev_src)
            replace_pass_by_copy_to_pass_by_ref(cpu_src)
            replace_pass_by_copy_to_pass_by_ref(header)

    generate_velocity_tendencies_h(sdfgs)

    final_main = main_name or (
        "main_gpu.cu" if gpu and not lib else "main.cc" if not gpu and not lib else None
    )
    if final_main:
        sources.add(final_main)

    use_nvhpc = os.getenv("_USE_NVHPC", "0").lower() in ("1", "true", "yes")
    # Headers are now in the root of the stage folder (build_loc.parent)
    base_inc = f"-I{build_loc.parent} -I{os.path.dirname(dace.__file__)}/runtime/include/ -Iinclude"

    # Use pkg-config to get correct paths for libraries loaded via Spack/Modules
    import subprocess

    extra_libs = ""
    for lib_name in ["sqlite3", "zlib", "libzstd"]:
        try:
            cflags = subprocess.check_output(
                ["pkg-config", "--cflags", lib_name], text=True
            ).strip()
            lflags = subprocess.check_output(
                ["pkg-config", "--libs", lib_name], text=True
            ).strip()
            base_inc += f" {cflags}"
            extra_libs += f" {lflags}"
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    if gpu:
        num = os.getenv("GENCODE_NUMBER", 0)
        arch = os.getenv("GENCODE_ARCH", f"arch=compute_{num},code=sm_{num}")
        if not num and not os.getenv("GENCODE_ARCH"):
            raise ValueError("GENCODE_NUMBER or GENCODE_ARCH must be set.")
        suppress = " ".join(
            [f"--diag-suppress {x}" for x in [68, 550, 20208, 1835, 177, 20012, 1098]]
        )
        nvhpc = "-ccbin=nvc++" if use_nvhpc else ""
        dbg = "-lineinfo"
        xcomp = f"-Xcompiler=-Wall -Xcompiler=-Wextra -Xcompiler=-Wno-unused-parameter {'' if use_nvhpc else '-Xcompiler=-Wconversion -Xcompiler=-Wno-sign-conversion -Xcompiler=-Wfloat-conversion -Xcompiler=-Wno-unknown-pragmas -Xcompiler=-faligned-new'}"
        if release:
            flags = f"{nvhpc} {suppress} {xcomp} -DNDEBUG -Xcompiler=-DNDEBUG -Xcompiler=-O3 --expt-relaxed-constexpr -gencode {arch} --use_fast_math -O3 {dbg} --ftz=true --prec-div=false --prec-sqrt=false --fmad=true -Xptxas=-O3 -Xptxas=-v -Xcompiler=-march=native -Xcompiler=-mtune=native --restrict -DNDEBUG"
        else:
            flags = f"{suppress} {xcomp} -DNDEBUG -Xcompiler=-Wall -Xcompiler=-Wextra --expt-relaxed-constexpr -gencode {arch} -O0 -Xcompiler=-O0 -G {dbg} --fmad=false --prec-div=true --prec-sqrt=true --ftz=false -DDACE_VELOCITY_DEBUG -Xcompiler=-DDACE_VELOCITY_DEBUG"
        if lib:
            flags += " -DNO_SERDE -DNVTX_DISABLE -std=c++17 -Xcompiler=-fPIC --compiler-options '-fPIC' --shared"
        else:
            flags += " -std=c++20"

        # Add linker wrappers for memory tracking
        flags += " -Xlinker --wrap=cudaMalloc -Xlinker --wrap=cudaFree"

        # Keep PTX/SASS intermediate files, organized by precision
        lowprec_tag = os.getenv("_LOWPREC", "fp64").lower()
        ptx_dir = f"ptx_out/{lowprec_tag}"
        flags += f" --keep --keep-dir={ptx_dir}"
        flags += f' -DLOWPREC_TAG=\\"{lowprec_tag}\\"'

        out_file = output_name or ("libvelocity_gpu.so" if lib else "velocity_gpu")
        cmd = f"nvcc {' '.join(sources)} {base_inc} {flags} {extra_libs} -lsqlite3 -lz -lzstd -o {out_file}"
    else:
        dbg = "-g" if debuginfo else ""
        if release:
            flags = f"{dbg} -std=c++20 -Wall -Wextra -Wno-unused-parameter -Wno-unused-variable -O3 -DNDEBUG"
        else:
            flags = f"-DDACE_VELOCITY_DEBUG -std=c++20 -Wall -Wextra -Wno-unused-parameter -Wno-unused-variable -Wno-unknown-pragmas -O0 -ggdb -fsanitize=address,undefined -fno-omit-frame-pointer {dbg}"

        out_file = output_name or ("libvelocity_cpu.so" if lib else "velocity_cpu")
        cmd = f"c++ {' '.join(sources)} {base_inc} {flags} {extra_libs} -lsqlite3 -lz -o {out_file}"

    if gpu:
        Path(ptx_dir).mkdir(parents=True, exist_ok=True)

    # Write recompile.<variant>.sh, then invoke it — single source of truth.
    # Derive variant suffix from output name, e.g.
    #   libvelocity_gpu_stage8_solve_nh_integration_release.f16.so → integration.f16
    _base = out_file.removesuffix(".so")
    _base = _base.removeprefix("lib")
    # Extract everything after "release" or "debug"
    for _tag in ("_release", "_debug"):
        _pos = _base.find(_tag)
        if _pos != -1:
            _variant = _base[_pos + len(_tag):].lstrip(".").replace(".", ".")
            break
    else:
        _variant = ""
    # e.g. "solve_nh_integration" part
    for _itag in ("_solve_nh_integration", "_standalone"):
        _ipos = _base.find(_itag)
        if _ipos != -1:
            _mode = _itag.lstrip("_").replace("solve_nh_", "")
            _variant = f"{_mode}.{_variant}" if _variant else _mode
            break
    recompile_name = f"recompile.{_variant}.sh" if _variant else "recompile.sh"
    recompile_sh = Path(recompile_name)
    script = f"#!/bin/sh\nset -e\nBINARY={out_file}\n\n{cmd}\n"
    if gpu:
        script += (
            f'\n# Dump SASS (native assembly) alongside PTX\n'
            f'echo "Dumping SASS to {ptx_dir}/ ..."\n'
            f'cuobjdump -sass "$BINARY" > {ptx_dir}/all_kernels.sass\n'
        )
    recompile_sh.write_text(script)
    recompile_sh.chmod(0o755)

    print(f"Compiling via ./{recompile_name}")
    if os.system(f"./{recompile_name}") != 0:
        print(f"\n❌ Build failed (see ./{recompile_name} for command)")
        exit(1)
    print(f"\n✅ Binary ready: ./{out_file}")
    return cmd
