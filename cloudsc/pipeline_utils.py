"""Shared helpers for the CPU and GPU codegen pipelines.

Kept intentionally small — just the byte-identical bits that were duplicated
between cloudsc_cpu_pipeline.py and cloudsc_gpu_pipeline.py.
"""
from __future__ import annotations

import os
import re


def repl_in_file(file_path: str, src: str, dst: str) -> None:
    """Substitute `src` with `dst` everywhere in `file_path`. No-op if the file is missing."""
    if not os.path.exists(file_path):
        return
    with open(file_path, "r") as f:
        code = f.read()
    with open(file_path, "w") as f:
        f.write(code.replace(src, dst))


def stabilize_interface(header_path: str, main_src: str) -> None:
    """Parse the generated DaCe header and rewrite call sites in `main_src` to match.

    Compile errors show up whenever DaCe's symbol extraction differs across SDFG
    variants; this keeps `cloudsc_main.{cpp,cu}` in sync with the baked header.
    """
    if not os.path.exists(header_path) or not os.path.exists(main_src):
        return

    with open(header_path) as f:
        header = f.read()

    h = re.sub(r"\s+", " ", header)

    m = re.search(r"__dace_init_cloudsc_py\(([^)]+)\)", h)
    if not m:
        print("  WARNING: could not parse __dace_init signature")
        return
    init_params = [p.strip().split()[-1] for p in m.group(1).split(",")]

    m = re.search(r"__program_cloudsc_py\(([^)]+)\)", h)
    if not m:
        print("  WARNING: could not parse __program signature")
        return
    prog_params = []
    for p in m.group(1).split(","):
        tokens = p.strip().replace("*", "").replace("__restrict__", "").split()
        prog_params.append(tokens[-1])

    init_call = f"__dace_init_cloudsc_py({', '.join(init_params)})"
    prog_call = f"__program_cloudsc_py({', '.join(prog_params)})"

    with open(main_src) as f:
        code = f.read()

    code = re.sub(r"__dace_init_cloudsc_py\([^)]+\)", init_call, code)
    code = re.sub(r"__program_cloudsc_py\([^)]+\)", prog_call, code)

    with open(main_src, "w") as f:
        f.write(code)

    print(f"  Stabilized interface: init({len(init_params)} args), program({len(prog_params)} args)")


_TMP_STRUCT_PATTERN = re.compile(r"^(\s*)int tmp_struct_symbol")


def modify_file(file_path: str) -> None:
    """Promote `int tmp_struct_symbol...;` declarations to `static` in-place."""
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    modified = False
    new_lines = []
    for line in lines:
        if _TMP_STRUCT_PATTERN.match(line) and "(" not in line and "," not in line and ";" in line:
            line = _TMP_STRUCT_PATTERN.sub(r"\1static int tmp_struct_symbol", line)
            modified = True
        new_lines.append(line)

    if modified:
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)


def modify_files_in_directory(directory) -> None:
    """Walk `directory`, apply `modify_file` to every .c/.h/.cpp/.cu source."""
    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            if file_path.endswith((".c", ".h", ".cpp", ".cu")):
                modify_file(file_path)
