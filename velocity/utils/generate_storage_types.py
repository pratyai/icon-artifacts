"""generate_storage_types.py — build-time header generation for GPU velocity.

Provides two functions called from compile_if_propagated_sdfgs.py:

patch_shared_struct_defs_h(sdfgs, include_dir)
    Reads include/shared_struct_defs.h.in (committed template, all pointer
    fields are double), substitutes concrete types from the SDFG array
    dtypes, and writes include/shared_struct_defs.h.

generate_velocity_tendencies_h(sdfgs, include_dir)
    Regenerates include/velocity_tendencies_no_nproma_gpu.h by merging
    the four DaCe-generated SDFG headers, stripping per-SDFG include
    guards and redundant shared includes.

Precision is controlled by the _LOWPREC env var (fp32/f32 → float,
fp16/f16 → dace::float16, anything else → double).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import dace

_REPO_ROOT = Path(__file__).parent.parent
_INCLUDE_DIR = _REPO_ROOT / "include"

# Matches any pointer-type field declaration we own, e.g.:
#   float *area_edge   double *coeff_gradekin   dace::float16 *geofac_rot
# Group 1 = current type, group 2 = field name.
_FIELD_TYPE_RE = re.compile(r"\b(dace::float16|float|double|__half)\s*\*\s*(\w+)")

# Patterns to strip from individual DaCe SDFG headers when merging them into
# the umbrella.  Everything else (struct forward-decls, DACE_EXPORTED decls)
# is kept verbatim.
_DACE_HDR_STRIP_RE = re.compile(
    r'#include\s+"shared_struct_defs\.h"\s*\n'
    r"|#ifndef\s+__DACE_CODEGEN_\w+__\s*\n"
    r"|#define\s+__DACE_CODEGEN_\w+__\s*\n"
    r"|#endif\s+//\s+__DACE_CODEGEN_\w+__\s*\n?"
    r"|#include\s+<dace/dace\.h>\s*\n",
    re.MULTILINE,
)


def _dace_to_cpp(dace_name: str) -> str:
    """Extract the C++ field name from a DaCe array name."""
    if "__m_" in dace_name:
        return dace_name.split("__m_")[-1]
    return dace_name


def _lowprec_cpp_type() -> str:
    """Return the concrete C++ type for the current _LOWPREC setting."""
    return {
        "fp32": "float",
        "f32": "float",
        "fp16": "dace::float16",
        "f16": "dace::float16",
    }.get(os.environ.get("_LOWPREC", "fp64").lower(), "double")


def patch_shared_struct_defs_h(sdfgs, include_dir: Path = _INCLUDE_DIR) -> None:
    """Generate shared_struct_defs.h from the .in template.

    Reads shared_struct_defs.h.in (committed, all pointer fields are double),
    substitutes concrete types based on actual SDFG array dtypes, and writes
    shared_struct_defs.h.
    """
    lowprec_type = _lowprec_cpp_type()

    # Map C++ field names to their actual precision in the SDFGs.
    is_lowprec: dict[str, bool] = {}
    for sdfg in sdfgs:
        for name, arr in sdfg.arrays.items():
            cpp_name = _dace_to_cpp(name)
            low = arr.dtype in (dace.float32, dace.float16)
            # If we see it low-precision in any SDFG, treat it as low-precision
            if cpp_name not in is_lowprec or low:
                is_lowprec[cpp_name] = low

            # Ensure z_ names are also explicitly mapped
            if name.startswith("z_"):
                if name not in is_lowprec or low:
                    is_lowprec[name] = low

    template = include_dir / "shared_struct_defs.h.in"
    content = template.read_text()

    # Substitute pointer types: template has double * everywhere,
    # replace with lowprec where the SDFG says the array is low-precision.
    def _replace(m: re.Match) -> str:
        field_name = m.group(2)
        new_type = lowprec_type if is_lowprec.get(field_name, False) else "double"
        return f"{new_type} *{field_name}"

    content = _FIELD_TYPE_RE.sub(_replace, content)

    # Generate z_ transient type aliases from SDFGs.
    all_z_names: set[str] = set()
    for sdfg in sdfgs:
        for name in sdfg.arrays:
            if name.startswith("z_"):
                all_z_names.add(name)

    alias_lines: list[str] = []
    for name in sorted(all_z_names):
        alias_type = lowprec_type if is_lowprec.get(name, False) else "double"
        alias_lines.append(f"using {name}_t = {alias_type};")

    content = content.replace("// @TRANSIENT_ALIASES@", "\n".join(alias_lines))

    out = include_dir / "shared_struct_defs.h"
    out.write_text(content)
    print(f"Generated {out} from {template}")


def generate_velocity_tendencies_h(sdfgs, include_dir: Path = _INCLUDE_DIR) -> None:
    """Regenerate velocity_tendencies_no_nproma_gpu.h from DaCe-generated SDFG headers.

    Strips per-SDFG include guards and redundant shared includes, then merges
    the four SDFG headers into a single umbrella.  Types come directly from
    DaCe (concrete float/double based on SDFG dtype) — no _storage_t aliases.
    """
    parts: list[str] = []
    for sdfg in sdfgs:
        hdr = Path(sdfg.build_folder) / "include" / f"{sdfg.name}.h"
        stripped = _DACE_HDR_STRIP_RE.sub("", hdr.read_text()).strip()
        if stripped:
            parts.append(stripped)

    lines: list[str] = [
        "// AUTO-GENERATED — do not edit manually.",
        "// Regenerated by: utils/generate_storage_types.py",
        "#ifndef __DACE_CODEGEN_VELOCITY_TENDENCIES__",
        "#define __DACE_CODEGEN_VELOCITY_TENDENCIES__",
        "",
        '#include "shared_struct_defs.h"',
        "#include <dace/dace.h>",
        "",
    ]
    for part in parts:
        lines.append(part)
        lines.append("")
    lines.append("#endif // __DACE_CODEGEN_VELOCITY_TENDENCIES__")
    lines.append("")

    out = include_dir / "velocity_tendencies_no_nproma_gpu.h"
    out.write_text("\n".join(lines))
    print(f"Generated {out}")


if __name__ == "__main__":
    raise SystemExit(
        "patch_shared_struct_defs_h requires SDFGs; run via the compile pipeline."
    )
