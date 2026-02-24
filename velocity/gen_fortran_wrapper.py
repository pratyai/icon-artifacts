#!/usr/bin/env python3
"""Generate a Fortran wrapper module for calling DaCe velocity_tendencies GPU library.

Parses the per-variant headers from codegen/stage8/ and produces a Fortran module
that uses serde.f90 glue types to call the DaCe-generated shared library.

Usage:
    python gen_fortran_wrapper.py [--stage-dir codegen/stage8]
                                  [--output velocity_gpu_wrapper.f90]
"""

import re
import argparse
from pathlib import Path

STAGE_DIR_DEFAULT = "codegen/stage8"
OUTPUT_DEFAULT = "velocity_gpu_wrapper.f90"

VARIANTS = [
    "velocity_no_nproma_if_prop_lvn_only_0_istep_1",
    "velocity_no_nproma_if_prop_lvn_only_0_istep_2",
    "velocity_no_nproma_if_prop_lvn_only_1_istep_1",
    "velocity_no_nproma_if_prop_lvn_only_1_istep_2",
]

# C type → Fortran iso_c_binding type
C_TO_FORTRAN = {
    "int": "INTEGER(c_int)",
    "double": "REAL(c_double)",
    "float": "REAL(c_float)",
    "uint8_t": "INTEGER(c_int8_t)",
}

# Struct types → passed as TYPE(c_ptr), VALUE
STRUCT_TYPES = {
    "global_data_type", "t_patch", "t_int_state",
    "t_nh_prog", "t_nh_metrics", "t_nh_diag",
}


def parse_init_params(header_text: str, variant: str) -> list[dict]:
    """Extract parameter list from __dace_init_<variant> declaration."""
    pattern = rf"__dace_init_{re.escape(variant)}\s*\((.*?)\);"
    m = re.search(pattern, header_text, re.DOTALL)
    if not m:
        raise ValueError(f"Could not find __dace_init_{variant}")

    raw = m.group(1).replace("\n", " ").replace("__restrict__", "")
    raw = re.sub(r"\s+", " ", raw).strip()

    params = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        is_ptr = "*" in chunk
        is_ref = "&" in chunk
        clean = chunk.replace("*", " ").replace("&", " ")
        clean = re.sub(r"\s+", " ", clean).strip()
        parts = clean.split()
        if len(parts) < 2:
            continue
        c_type, name = parts[0], parts[-1]
        params.append({
            "name": name,
            "c_type": c_type,
            "is_pointer": is_ptr,
            "is_ref": is_ref,
            "is_struct": c_type in STRUCT_TYPES,
        })
    return params


def param_to_fortran_decl(p: dict) -> str:
    """Convert a parsed param to Fortran INTERFACE declaration."""
    name = p["name"]
    if p["is_struct"] or p["is_pointer"]:
        return f"      TYPE(c_ptr), VALUE :: {name}"
    if p["is_ref"]:
        ft = C_TO_FORTRAN.get(p["c_type"], "INTEGER(c_int)")
        return f"      {ft} :: {name}  ! by reference (C++ &)"
    ft = C_TO_FORTRAN.get(p["c_type"], "INTEGER(c_int)")
    return f"      {ft}, VALUE :: {name}"


def param_to_arg_expr(p: dict) -> str:
    """Convert a parsed param to the actual argument expression in the call."""
    name = p["name"]

    if p["is_struct"]:
        # Map C struct type to glue variable name
        struct_to_glue = {
            "global_data": "g_global",
            "p_diag": "g_diag",
            "p_int": "g_int",
            "p_metrics": "g_metrics",
            "p_patch": "g_patch",
            "p_prog": "g_prog",
        }
        return f"c_loc({struct_to_glue[name]})"

    if name.startswith("z_") and p["is_pointer"]:
        return f"c_loc({name}(1,1,1))"

    if p["is_ref"]:
        return name

    # __f2dace_A_* → SIZE(array, dim)
    m = re.match(r"__f2dace_A_(.+)_d_(\d+)_s$", name)
    if m:
        arr, dim = m.group(1), int(m.group(2)) + 1
        return f"INT(SIZE({arr}, {dim}), c_int)"

    # __f2dace_OA_* → LBOUND(array, dim)
    m = re.match(r"__f2dace_OA_(.+)_d_(\d+)_s$", name)
    if m:
        arr, dim = m.group(1), int(m.group(2)) + 1
        return f"INT(LBOUND({arr}, {dim}), c_int)"

    # Named scalars
    scalar_map = {
        "istep": "c_istep", "ntnd": "c_ntnd",
        "lvn_only": "c_lvn_only", "ldeepatmo": "c_ldeepatmo",
        "dtime": "c_dtime", "dt_linintp_ubc": "c_dt_linintp_ubc",
    }
    if name in scalar_map:
        return scalar_map[name]

    return name


def emit_interface(variant: str, params: list[dict]) -> list[str]:
    """Generate INTERFACE block for init/program/exit of one variant."""
    lines = []
    pnames = [p["name"] for p in params]

    # __dace_init
    init = f"__dace_init_{variant}"
    lines.append(f"    FUNCTION {init}( &")
    for i, n in enumerate(pnames):
        lines.append(f"        {n}{',' if i < len(pnames)-1 else ''} &")
    lines.append(f"    ) RESULT(state_ptr) BIND(C, NAME='{init}')")
    lines.append(f"      USE, INTRINSIC :: iso_c_binding")
    lines.append(f"      IMPLICIT NONE")
    for p in params:
        lines.append(param_to_fortran_decl(p))
    lines.append(f"      TYPE(c_ptr) :: state_ptr")
    lines.append(f"    END FUNCTION {init}")
    lines.append("")

    # __program
    prog = f"__program_{variant}"
    lines.append(f"    SUBROUTINE {prog}( &")
    lines.append(f"        state_ptr, &")
    for i, n in enumerate(pnames):
        lines.append(f"        {n}{',' if i < len(pnames)-1 else ''} &")
    lines.append(f"    ) BIND(C, NAME='{prog}')")
    lines.append(f"      USE, INTRINSIC :: iso_c_binding")
    lines.append(f"      IMPLICIT NONE")
    lines.append(f"      TYPE(c_ptr), VALUE :: state_ptr")
    for p in params:
        lines.append(param_to_fortran_decl(p))
    lines.append(f"    END SUBROUTINE {prog}")
    lines.append("")

    # __dace_exit
    exit_ = f"__dace_exit_{variant}"
    lines.append(f"    FUNCTION {exit_}(state_ptr) RESULT(rc) BIND(C, NAME='{exit_}')")
    lines.append(f"      USE, INTRINSIC :: iso_c_binding")
    lines.append(f"      IMPLICIT NONE")
    lines.append(f"      TYPE(c_ptr), VALUE :: state_ptr")
    lines.append(f"      INTEGER(c_int) :: rc")
    lines.append(f"    END FUNCTION {exit_}")

    return lines


def emit_call_args(params: list[dict], indent: str) -> list[str]:
    """Emit argument expressions for a DaCe function call."""
    lines = []
    for i, p in enumerate(params):
        arg = param_to_arg_expr(p)
        comma = ", &" if i < len(params) - 1 else " &"
        lines.append(f"{indent}{arg}{comma}")
    return lines


def generate(all_params: dict[str, list[dict]]) -> str:
    """Generate the full Fortran wrapper module."""
    L = []  # output lines

    L.append("! AUTO-GENERATED by gen_fortran_wrapper.py — do not edit manually.")
    L.append("! Provides velocity_tendencies_gpu as a drop-in replacement for")
    L.append("! velocity_tendencies, calling the DaCe GPU shared library.")
    L.append("MODULE vt_wrapper")
    L.append("  USE, INTRINSIC :: iso_c_binding")
    L.append("  USE f90_glue_vt_serde, ONLY: &")
    L.append("    glue_global_data_type, glue_t_patch, glue_t_int_state, &")
    L.append("    glue_t_nh_prog, glue_t_nh_metrics, glue_t_nh_diag, &")
    L.append("    ctor")
    L.append("  USE mo_model_domain, ONLY: t_patch")
    L.append("  USE mo_intp_data_strc, ONLY: t_int_state")
    L.append("  USE mo_nonhydro_types, ONLY: t_nh_prog, t_nh_metrics, t_nh_diag")
    L.append("  USE mo_kind, ONLY: wp, vp")
    L.append("  USE mo_parallel_config, ONLY: nproma")
    L.append("  USE mo_init_vgrid, ONLY: nflatlev")
    L.append("  USE mo_mpi, ONLY: i_am_accel_node")
    L.append("  USE mo_nonhydrostatic_config, ONLY: lextra_diffu")
    L.append("  USE mo_run_config, ONLY: timers_level")
    L.append("  USE mo_timer, ONLY: timer_intp, timer_solve_nh_veltend")
    L.append("  USE mo_vertical_grid, ONLY: nrdmax")
    L.append("  IMPLICIT NONE")
    L.append("  PRIVATE")
    L.append("  PUBLIC :: velocity_tendencies_gpu, velocity_gpu_init, velocity_gpu_finalize")
    L.append("")
    # Module-level state pointers
    for v in VARIANTS:
        s = v.replace("velocity_no_nproma_if_prop_", "")
        L.append(f"  TYPE(c_ptr) :: state_{s} = c_null_ptr")
    L.append("")

    # INTERFACE blocks
    L.append("  INTERFACE")
    for v in VARIANTS:
        L.append("")
        L.extend(emit_interface(v, all_params[v]))
        L.append("")
    L.append("  END INTERFACE")
    L.append("")
    L.append("CONTAINS")
    L.append("")

    # ---- velocity_gpu_init ----
    L.append("  SUBROUTINE velocity_gpu_init()")
    L.append("    ! Placeholder — actual init happens lazily on first call per variant.")
    L.append("  END SUBROUTINE velocity_gpu_init")
    L.append("")

    # ---- velocity_gpu_finalize ----
    L.append("  SUBROUTINE velocity_gpu_finalize()")
    L.append("    INTEGER(c_int) :: rc")
    for v in VARIANTS:
        s = v.replace("velocity_no_nproma_if_prop_", "")
        L.append(f"    IF (C_ASSOCIATED(state_{s})) THEN")
        L.append(f"      rc = __dace_exit_{v}(state_{s})")
        L.append(f"      state_{s} = c_null_ptr")
        L.append(f"    END IF")
    L.append("  END SUBROUTINE velocity_gpu_finalize")
    L.append("")

    # ---- init_global_data ----
    L.append("  SUBROUTINE init_global_data(g)")
    L.append("    TYPE(glue_global_data_type), INTENT(OUT) :: g")
    L.append("    INTEGER(c_int), ALLOCATABLE, TARGET, SAVE :: a_nflatlev(:), a_nrdmax(:)")
    L.append("    IF (.NOT. ALLOCATED(a_nflatlev)) THEN")
    L.append("      ALLOCATE(a_nflatlev(1))")
    L.append("      a_nflatlev = INT(nflatlev, c_int)")
    L.append("    END IF")
    L.append("    IF (.NOT. ALLOCATED(a_nrdmax)) THEN")
    L.append("      ALLOCATE(a_nrdmax(1))")
    L.append("      a_nrdmax = INT(nrdmax, c_int)")
    L.append("    END IF")
    L.append("    ! Fields used by velocity_tendencies (rest is zero from INTENT(OUT))")
    L.append("    g%m_nflatlev = c_loc(a_nflatlev)")
    L.append("    g%m_nrdmax = c_loc(a_nrdmax)")
    L.append("    g%m_nproma = INT(nproma, c_int)")
    L.append("    g%m_i_am_accel_node = MERGE(1_c_int, 0_c_int, i_am_accel_node)")
    L.append("    g%m_lextra_diffu = MERGE(1_c_int, 0_c_int, lextra_diffu)")
    L.append("    g%m_timers_level = INT(timers_level, c_int)")
    L.append("    g%m_timer_intp = INT(timer_intp, c_int)")
    L.append("    g%m_timer_solve_nh_veltend = INT(timer_solve_nh_veltend, c_int)")
    L.append("  END SUBROUTINE init_global_data")
    L.append("")

    # ---- velocity_tendencies_gpu ----
    L.append("  SUBROUTINE velocity_tendencies_gpu(p_prog, p_patch, p_int, p_metrics, p_diag, &")
    L.append("      z_w_concorr_me, z_kin_hor_e, z_vt_ie, ntnd, istep, lvn_only, &")
    L.append("      dtime, dt_linintp_ubc, ldeepatmo)")
    L.append("    TYPE(t_nh_prog), INTENT(INOUT), TARGET :: p_prog")
    L.append("    TYPE(t_patch), INTENT(IN), TARGET :: p_patch")
    L.append("    TYPE(t_int_state), INTENT(IN), TARGET :: p_int")
    L.append("    TYPE(t_nh_metrics), INTENT(INOUT), TARGET :: p_metrics")
    L.append("    TYPE(t_nh_diag), INTENT(INOUT), TARGET :: p_diag")
    L.append("    REAL(vp), DIMENSION(:,:,:), INTENT(INOUT), TARGET :: z_w_concorr_me, z_kin_hor_e, z_vt_ie")
    L.append("    INTEGER, INTENT(IN) :: ntnd, istep")
    L.append("    LOGICAL, INTENT(IN) :: lvn_only, ldeepatmo")
    L.append("    REAL(wp), INTENT(IN) :: dtime, dt_linintp_ubc")
    L.append("")
    L.append("    ! Glue structs (C-compatible deep copies)")
    L.append("    TYPE(glue_t_patch), TARGET :: g_patch")
    L.append("    TYPE(glue_t_int_state), TARGET :: g_int")
    L.append("    TYPE(glue_t_nh_prog), TARGET :: g_prog")
    L.append("    TYPE(glue_t_nh_metrics), TARGET :: g_metrics")
    L.append("    TYPE(glue_t_nh_diag), TARGET :: g_diag")
    L.append("    TYPE(glue_global_data_type), TARGET :: g_global")
    L.append("")
    L.append("    ! Scalar conversions")
    L.append("    INTEGER(c_int) :: c_istep, c_ntnd, c_lvn_only, c_ldeepatmo")
    L.append("    REAL(c_double) :: c_dtime, c_dt_linintp_ubc")
    L.append("")
    L.append("    ! For copy-back of p_diag output arrays")
    L.append("    REAL(c_double), POINTER :: ptr_ddt_vn(:,:,:,:)")
    L.append("    REAL(c_double), POINTER :: ptr_ddt_w(:,:,:,:)")
    L.append("    REAL(c_double), POINTER :: ptr_vn_ie(:,:,:)")
    L.append("    REAL(c_double), POINTER :: ptr_vt(:,:,:)")
    L.append("    REAL(c_double), POINTER :: ptr_w_concorr_c(:,:,:)")
    L.append("")
    L.append("    CALL timer_start(timer_solve_nh_veltend)")
    L.append("")
    L.append("    ! Pack Fortran structs into C-compatible glue types")
    L.append("    CALL ctor(p_patch, g_patch, .TRUE.)")
    L.append("    CALL ctor(p_int, g_int, .TRUE.)")
    L.append("    CALL ctor(p_prog, g_prog, .TRUE.)")
    L.append("    CALL ctor(p_metrics, g_metrics, .TRUE.)")
    L.append("    CALL ctor(p_diag, g_diag, .TRUE.)")
    L.append("    CALL init_global_data(g_global)")
    L.append("")
    L.append("    ! Convert scalars")
    L.append("    c_istep = INT(istep, c_int)")
    L.append("    c_ntnd = INT(ntnd, c_int)")
    L.append("    c_lvn_only = MERGE(1_c_int, 0_c_int, lvn_only)")
    L.append("    c_ldeepatmo = MERGE(1_c_int, 0_c_int, ldeepatmo)")
    L.append("    c_dtime = REAL(dtime, c_double)")
    L.append("    c_dt_linintp_ubc = REAL(dt_linintp_ubc, c_double)")
    L.append("")

    # Dispatch
    for i, v in enumerate(VARIANTS):
        params = all_params[v]
        s = v.replace("velocity_no_nproma_if_prop_", "")
        m = re.search(r"lvn_only_(\d+)_istep_(\d+)", v)
        assert m is not None
        lvn_val, istep_val = m.group(1), m.group(2)
        lvn_bool = ".FALSE." if lvn_val == "0" else ".TRUE."
        prefix = "IF" if i == 0 else "ELSE IF"

        L.append(f"    {prefix} (lvn_only .EQV. {lvn_bool} .AND. istep == {istep_val}) THEN")
        L.append(f"      IF (.NOT. C_ASSOCIATED(state_{s})) THEN")
        L.append(f"        state_{s} = __dace_init_{v}( &")
        L.extend(emit_call_args(params, "          "))
        L.append(f"        )")
        L.append(f"      END IF")
        L.append(f"      CALL __program_{v}( &")
        L.append(f"        state_{s}, &")
        L.extend(emit_call_args(params, "        "))
        L.append(f"      )")
        L.append("")

    L.append("    END IF")
    L.append("")

    # Copy-back: p_diag output arrays
    L.append("    ! ---- Copy results back from glue structs to Fortran types ----")
    L.append("    ! The DaCe library wrote into the deep-copied arrays inside g_diag.")
    L.append("    ! We need to copy them back into the original Fortran p_diag fields.")
    L.append("")
    L.append("    ! max_vcfl_dyn (scalar field in glue struct, modified by DaCe)")
    L.append("    p_diag%max_vcfl_dyn = g_diag%m_max_vcfl_dyn")
    L.append("")

    # Generate copy-back for each p_diag array field
    diag_fields = [
        ("ddt_vn_apc_pc", "ptr_ddt_vn", 4,
         ["g_diag%m___f2dace_SA_ddt_vn_apc_pc_d_0_s_300",
          "g_diag%m___f2dace_SA_ddt_vn_apc_pc_d_1_s_301",
          "g_diag%m___f2dace_SA_ddt_vn_apc_pc_d_2_s_302",
          "g_diag%m___f2dace_SA_ddt_vn_apc_pc_d_3_s_303"]),
        ("ddt_w_adv_pc", "ptr_ddt_w", 4,
         ["g_diag%m___f2dace_SA_ddt_w_adv_pc_d_0_s_304",
          "g_diag%m___f2dace_SA_ddt_w_adv_pc_d_1_s_305",
          "g_diag%m___f2dace_SA_ddt_w_adv_pc_d_2_s_306",
          "g_diag%m___f2dace_SA_ddt_w_adv_pc_d_3_s_307"]),
        ("vn_ie", "ptr_vn_ie", 3,
         ["g_diag%m___f2dace_SA_vn_ie_d_0_s_294",
          "g_diag%m___f2dace_SA_vn_ie_d_1_s_295",
          "g_diag%m___f2dace_SA_vn_ie_d_2_s_296"]),
        ("vt", "ptr_vt", 3,
         ["g_diag%m___f2dace_SA_vt_d_0_s_291",
          "g_diag%m___f2dace_SA_vt_d_1_s_292",
          "g_diag%m___f2dace_SA_vt_d_2_s_293"]),
        ("w_concorr_c", "ptr_w_concorr_c", 3,
         ["g_diag%m___f2dace_SA_w_concorr_c_d_0_s_297",
          "g_diag%m___f2dace_SA_w_concorr_c_d_1_s_298",
          "g_diag%m___f2dace_SA_w_concorr_c_d_2_s_299"]),
    ]

    for field, ptr_name, _ndim, shape_exprs in diag_fields:
        shape_str = ", ".join(shape_exprs)
        L.append(f"    CALL c_f_pointer(g_diag%m_{field}, {ptr_name}, [{shape_str}])")
        L.append(f"    p_diag%{field} = {ptr_name}")
        L.append("")

    L.append("  END SUBROUTINE velocity_tendencies_gpu")
    L.append("")
    L.append("END MODULE vt_wrapper")
    return "\n".join(L)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-dir", default=STAGE_DIR_DEFAULT)
    parser.add_argument("--output", default=OUTPUT_DEFAULT)
    args = parser.parse_args()

    all_params = {}
    for v in VARIANTS:
        header_path = Path(args.stage_dir) / v / "include" / f"{v}.h"
        header_text = header_path.read_text()
        params = parse_init_params(header_text, v)
        all_params[v] = params
        print(f"Parsed {v}: {len(params)} parameters")

    fortran = generate(all_params)
    Path(args.output).write_text(fortran)
    print(f"Generated {args.output} ({len(fortran.splitlines())} lines)")


if __name__ == "__main__":
    main()
