"""text_patches.py — Post-codegen text-level patches for CloudSC.

Ported from velocity/utils/compile_if_propagated_sdfgs.py.
Applied to generated C++ source after DaCe codegen, before compilation.
"""

from __future__ import annotations

import re
from pathlib import Path


def fix_mixed_precision_ambiguity(file_path: Path):
    """Inject fp16_operators.h to resolve double/half operator ambiguity.

    Ported from velocity/utils/compile_if_propagated_sdfgs.py.
    """
    with open(file_path, "r") as f:
        content = f.read()

    include = '#include "fp16_operators.h"'
    if include in content:
        return

    new_content = re.sub(
        r"(#include\s+[<\"][^>\"]+[>\"])",
        rf"\1\n{include}",
        content,
        count=1,
    )

    if new_content != content:
        with open(file_path, "w") as f:
            f.write(new_content)


def floatify_generated_code(file_path: Path):
    """Replace double literals in generated C++ with float equivalents.

    DaCe codegen emits double literals for interstate edge conditions
    and other places that the SDFG-level floatify doesn't reach.
    Ported from compile_if_propagated_sdfgs.py.
    """
    with open(file_path, "r") as f:
        code = f.read()

    new_code = code
    for val in ["0.85", "0.5", "1.0", "0.0", "0.05", "0.65", "1.15"]:
        new_code = re.sub(
            rf"(?<!float\()(?<![0-9]){re.escape(val)}(?!f)(?![0-9])",
            f"{val}f",
            new_code,
        )

    # ipow(x, 2) -> (x * x) — safety net for anything codegen emitted
    new_code = re.sub(
        r"dace::math::ipow\(([^,]+),\s*2\)",
        r"((\1) * (\1))",
        new_code,
    )

    if new_code != code:
        with open(file_path, "w") as f:
            f.write(new_code)


# TODO: BFP text patching
# def patch_bfp_reads(code, bfp_gpu_names, block_size=32, mantissa_bits=16):
#     """Text-level BFP patching: fix parameter types and replace array reads
#     with bfp_decode calls.  Ported from velocity."""
#     ...


def inject_copy_cast(file_path: Path):
    """Inject copy_cast.h after dace/copy.h to handle mixed-type CopyND calls."""
    with open(file_path, "r") as f:
        content = f.read()

    include = '#include "copy_cast.h"'
    if include in content:
        return

    # Insert after the dace include
    new_content = re.sub(
        r'(#include\s+<dace/dace\.h>)',
        rf'\1\n{include}',
        content,
        count=1,
    )

    if new_content != content:
        with open(file_path, "w") as f:
            f.write(new_content)


def inject_host_timer_around_compute(file_path: Path):
    """Wrap the kernel-only region of __program_cloudsc_py_internal with a
    host-side wall-clock chrono timer (VT-style).

    Anchor: the FIRST and LAST __dace_runkernel_* call sites inside the body
    of __program_cloudsc_py_internal. Kernels live strictly between the
    entry copyin block and the exit copyout block, so this is robust to mid-
    program refresh-state H2D/D2H insertions.

    Layout produced (per call of __program_cloudsc_py_internal):

        // ... entry copyin H2D block ...
        cudaStreamSynchronize(...);
        auto cloudsc_compute_t_start = chrono::now();
        __dace_runkernel_<first>(...);
        // ... compute, with mid-program refreshes ...
        __dace_runkernel_<last>(...);
        cudaStreamSynchronize(...);
        { auto stop = chrono::now(); print elapsed; }
        // ... exit copyout D2H block ...
    """
    if file_path.suffix not in (".cu", ".cpp"):
        return

    with open(file_path, "r") as f:
        text = f.read()

    if "cloudsc_compute_t_start" in text:
        return  # already patched

    lines = text.splitlines(keepends=True)
    n = len(lines)

    # Find the body range of __program_cloudsc_py_internal.
    body_start = None
    for i, line in enumerate(lines):
        if "void __program_cloudsc_py_internal(" in line:
            body_start = i
            break
    if body_start is None:
        return
    # Function body opening brace: walk forward from body_start past the
    # signature until we find the line ending with "{".
    while body_start < n and not lines[body_start].rstrip().endswith("{"):
        body_start += 1
    if body_start >= n:
        return
    body_start += 1  # first line of body

    # End of body: matching closing brace at column 0.
    body_end = body_start
    depth = 1
    while body_end < n and depth > 0:
        for ch in lines[body_end]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        body_end += 1
    # body_end is now exclusive end (line after the closing })

    # Find first / last __dace_runkernel_<...>( call site within body.
    first_kernel = None
    last_kernel = None
    for i in range(body_start, body_end):
        line = lines[i]
        # Skip declarations like "DACE_EXPORTED void __dace_runkernel_..." —
        # those are at file top, not in __program_internal.
        if "__dace_runkernel_" in line and "(" in line:
            if first_kernel is None:
                first_kernel = i
            last_kernel = i
    if first_kernel is None or last_kernel is None:
        return

    # Walk forward from last_kernel to the line containing the closing ");"
    # of that call statement (kernel calls span multiple lines).
    end_of_last = last_kernel
    while end_of_last < body_end and ");" not in lines[end_of_last]:
        end_of_last += 1
    end_of_last += 1  # insertion happens AFTER this line

    start_block = (
        "    cudaStreamSynchronize(__state->gpu_context->streams[0]);\n"
        "    auto cloudsc_compute_t_start = std::chrono::high_resolution_clock::now();\n"
    )
    stop_block = (
        "    cudaStreamSynchronize(__state->gpu_context->streams[0]);\n"
        "    {\n"
        "        auto cloudsc_compute_t_stop = std::chrono::high_resolution_clock::now();\n"
        "        double cloudsc_compute_us = std::chrono::duration<double, std::micro>(\n"
        "            cloudsc_compute_t_stop - cloudsc_compute_t_start).count();\n"
        "        std::cerr << \"[cloudsc] H2D->D2H wall time: \" << cloudsc_compute_us << \" us\\n\";\n"
        "    }\n"
    )

    # Insert in reverse order so the first index isn't shifted by the second.
    lines.insert(end_of_last, stop_block)
    lines.insert(first_kernel, start_block)

    out = "".join(lines)
    if "#include <chrono>" not in out:
        out = "#include <chrono>\n" + out
    if "#include <iostream>" not in out:
        out = "#include <iostream>\n" + out
    with open(file_path, "w") as f:
        f.write(out)


def comment_out_syncs(file_path: Path):
    """Comment out conservative CUDA synchronization calls to allow kernel overlap.

    Ported from velocity/utils/compile_if_propagated_sdfgs.py.
    """
    with open(file_path, "r") as f:
        lines = f.readlines()

    modified = False
    new_lines = []
    for line in lines:
        # Match typical CUDA sync/event primitives
        if any(x in line for x in ["cudaStreamSynchronize", "EventRecord", "StreamWaitEvent"]):
            # Don't comment out syncs used for our own timers/profiling if they exist
            if not any(x in line for x in ["stop", "start"]):
                line = "//" + line
                modified = True
        new_lines.append(line)

    if modified:
        with open(file_path, "w") as f:
            f.writelines(new_lines)


def apply_text_patches(codegen_dir: Path, lowprec: str):
    """Apply all text-level patches to generated source files.

    Args:
        codegen_dir: Path to the codegen/ directory with generated .cpp/.h files.
        lowprec: Precision mode string.
    """
    source_files = list(codegen_dir.glob("*.cpp")) + list(codegen_dir.glob("*.h")) + list(codegen_dir.glob("*.cu"))

    for f in source_files:
        # Host-side wall-clock timer applies regardless of precision.
        inject_host_timer_around_compute(f)

        if lowprec in ("fp64", "f64"):
            continue

        if lowprec in ("fp16", "f16"):
            fix_mixed_precision_ambiguity(f)
        inject_copy_cast(f)
        floatify_generated_code(f)

    print(f"Applied text patches to {len(source_files)} files in {codegen_dir}")
