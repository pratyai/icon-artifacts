#!/usr/bin/env python3
"""Generate a Fortran wrapper module for calling DaCe velocity_tendencies GPU library.

Integration-mode wrapper: individual struct field pointers are passed as GPU
device pointers via OpenACC HOST_DATA USE_DEVICE.

Usage:
    python gen_fortran_wrapper.py [--stage-dir codegen/stage8/f64]
                                  [--output velocity_gpu_wrapper.f90]
                                  [--serde]        # include serialization hooks
                                  [--debug-prints]  # include GPU pointer debug prints
"""

from __future__ import annotations

import re
import argparse
from pathlib import Path

STAGE_DIR_DEFAULT = "codegen/stage8/f64"
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
    "global_data_type",
    "t_patch",
    "t_int_state",
    "t_nh_prog",
    "t_nh_metrics",
    "t_nh_diag",
}


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

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
        # Strip __abi_ prefix
        if name.startswith("__abi_"):
            name = name[len("__abi_"):]
        # Strip _fp64 suffix — Fortran always passes double, BIND(C) matches
        # by position not name.  This makes the wrapper precision-agnostic.
        if name.endswith("_fp64"):
            name = name[:-len("_fp64")]
        params.append(
            {
                "name": name,
                "c_type": c_type,
                "is_pointer": is_ptr,
                "is_ref": is_ref,
                "is_struct": c_type in STRUCT_TYPES,
            }
        )
    return params


# ---------------------------------------------------------------------------
# INTERFACE declarations
# ---------------------------------------------------------------------------

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


def emit_interface(variant: str, params: list[dict]) -> list[str]:
    """Generate INTERFACE block for init/program/exit of one variant."""
    lines = []
    pnames = [p["name"] for p in params]

    # __dace_init
    init = f"__dace_init_{variant}"
    lines.append(f"    FUNCTION {init}( &")
    for i, n in enumerate(pnames):
        lines.append(f"        {n}{',' if i < len(pnames) - 1 else ''} &")
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
        lines.append(f"        {n}{',' if i < len(pnames) - 1 else ''} &")
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
    lines.append(
        f"    FUNCTION {exit_}(state_ptr) RESULT(rc) BIND(C, NAME='{exit_}')"
    )
    lines.append(f"      USE, INTRINSIC :: iso_c_binding")
    lines.append(f"      IMPLICIT NONE")
    lines.append(f"      TYPE(c_ptr), VALUE :: state_ptr")
    lines.append(f"      INTEGER(c_int) :: rc")
    lines.append(f"    END FUNCTION {exit_}")

    return lines


# ---------------------------------------------------------------------------
# Call-site argument mapping
# ---------------------------------------------------------------------------

def _cg_to_fortran_field(name: str) -> str:
    """Convert __CG_ / gpu___CG_ parameter name to Fortran struct%field.

    Examples:
        __CG_p_patch__CG_cells__m_start_block
            → p_patch%cells%start_block
        gpu___CG_p_diag__m_vn_ie_fp64
            → p_diag%vn_ie
        gpu___CG_p_patch__CG_cells__CG_decomp_info__m_owner_mask
            → p_patch%cells%decomp_info%owner_mask
    """
    s = name
    if s.startswith("gpu_"):
        s = s[len("gpu_"):]
    if s.endswith("_fp64"):
        s = s[: -len("_fp64")]
    # Strip leading __CG_
    s = re.sub(r"^_+CG_", "", s)
    # __CG_ (nested struct) → %
    s = s.replace("__CG_", "%")
    # __m_ (field access) → %
    s = s.replace("__m_", "%")
    return s


def _compute_z_ranks(params: list[dict]) -> dict[str, int]:
    """Determine rank of each z_ array from __f2dace_OA_ params."""
    ranks: dict[str, int] = {}
    for p in params:
        m = re.match(r"__f2dace_OA_(.+)_d_(\d+)_s$", p["name"])
        if m:
            arr = m.group(1)
            dim = int(m.group(2)) + 1
            ranks[arr] = max(ranks.get(arr, 0), dim)
    return ranks


def param_to_arg_expr(p: dict, z_ranks: dict[str, int] | None = None) -> str:
    """Convert a parsed C param to the Fortran argument expression at the call site."""
    name = p["name"]

    # GPU device pointers: gpu___CG_*
    # Inside !$ACC HOST_DATA USE_DEVICE block, c_loc gives device pointer
    if name.startswith("gpu___CG_"):
        field = _cg_to_fortran_field(name)
        return f"c_loc({field})"

    # Host global_data helper arrays
    if name == "__CG_global_data__m_nflatlev":
        return "c_loc(a_nflatlev)"
    if name == "__CG_global_data__m_nrdmax":
        return "c_loc(a_nrdmax)"

    # Host p_patch topology arrays (1D: indexed by refin_ctrl level)
    # These arrays have negative LBOUNDs (e.g., verts: -7, edges: -13).
    # DaCe C++ uses SOA = LBOUND, so the pointer must start at LBOUND,
    # not at index 1 — otherwise C++ computes ptr[idx - LBOUND] past the
    # actual array end.
    if name.startswith("__CG_p_patch__CG_"):
        field = _cg_to_fortran_field(name)
        return f"c_loc({field}(LBOUND({field}, 1)))"

    # global_data struct pointer
    if name == "global_data" and p["is_struct"]:
        return "c_loc(g_global)"

    # p_patch struct pointer (code reads nblks_c/e/v)
    if name == "p_patch" and p["is_struct"]:
        return "c_loc(g_patch)"

    # Struct pointers — generated code dereferences these for shape metadata
    _struct_to_glue = {
        "p_diag": "g_diag",
        "p_int": "g_int",
        "p_metrics": "g_metrics",
        "p_prog": "g_prog",
    }
    if p["is_struct"] and name in _struct_to_glue:
        return f"c_loc({_struct_to_glue[name]})"

    # z_ GPU device pointers (z_*_fp64 or z_* without suffix)
    # Inside !$ACC HOST_DATA USE_DEVICE block, c_loc gives device pointer
    # Must index first element — NVFORTRAN rejects c_loc on assumed-shape arrays
    if name.startswith("z_") and p["is_pointer"]:
        if name.endswith("_fp64"):
            base = name[: -len("_fp64")]
        else:
            base = name
        rank = (z_ranks or {}).get(base, 3)  # default rank 3
        lbounds = ",".join([f"LBOUND({base}, {d+1})" for d in range(rank)])
        return f"c_loc({base}({lbounds}))"

    # __CG_global_data__m_nproma scalar
    if name == "__CG_global_data__m_nproma":
        return "INT(nproma, c_int)"

    # __CG_p_diag__m_max_vcfl_dyn (by reference)
    if name == "__CG_p_diag__m_max_vcfl_dyn":
        return "c_max_vcfl_dyn"

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
        "istep": "c_istep",
        "ntnd": "c_ntnd",
        "lvn_only": "c_lvn_only",
        "ldeepatmo": "c_ldeepatmo",
        "dtime": "c_dtime",
        "dt_linintp_ubc": "c_dt_linintp_ubc",
    }
    if name in scalar_map:
        return scalar_map[name]

    return name


def emit_call_args(params: list[dict], indent: str) -> list[str]:
    """Emit argument expressions for a DaCe function call."""
    z_ranks = _compute_z_ranks(params)
    lines = []
    for i, p in enumerate(params):
        arg = param_to_arg_expr(p, z_ranks)
        comma = ", &" if i < len(params) - 1 else " &"
        lines.append(f"{indent}{arg}{comma}")
    return lines


# ---------------------------------------------------------------------------
# Module generation
# ---------------------------------------------------------------------------

def generate(all_params: dict[str, list[dict]], *,
             serde: bool = False, debug_prints: bool = False) -> str:
    """Generate the full Fortran wrapper module.

    Args:
        serde: Include serialization instrumentation (before/after DaCe calls).
        debug_prints: Include GPU pointer debug prints for troubleshooting.
    """
    L: list[str] = []

    L.append("! AUTO-GENERATED by gen_fortran_wrapper.py — do not edit manually.")
    L.append(
        "! Integration-mode wrapper: passes GPU device pointers via OpenACC."
    )
    L.append("MODULE vt_wrapper")
    L.append("  USE, INTRINSIC :: iso_c_binding")
    L.append("  USE f90_glue_vt_serde, ONLY: glue_t_patch, glue_t_int_state, &")
    L.append("    glue_t_nh_prog, glue_t_nh_metrics, glue_t_nh_diag, ctor")
    L.append("  USE mo_model_domain, ONLY: t_patch")
    L.append("  USE mo_intp_data_strc, ONLY: t_int_state")
    L.append("  USE mo_nonhydro_types, ONLY: t_nh_prog, t_nh_metrics, t_nh_diag")
    L.append("  USE mo_kind, ONLY: wp, vp")
    L.append("  USE mo_parallel_config, ONLY: nproma")
    L.append("  USE mo_init_vgrid, ONLY: nflatlev")
    L.append("  USE mo_vertical_grid, ONLY: nrdmax")
    L.append(
        "  USE mo_timer, ONLY: timer_solve_nh_veltend, timer_start, timer_stop"
    )
    L.append("  IMPLICIT NONE")
    L.append("  PRIVATE")
    L.append(
        "  PUBLIC :: velocity_tendencies_gpu, velocity_gpu_init, velocity_gpu_finalize"
    )
    L.append("")

    # ----------------------------------------------------------------
    # Lightweight BIND(C) types matching the C struct layouts.
    # Only fields actually dereferenced by the generated CUDA code need
    # correct values; the rest are zero-initialized placeholders.
    # ----------------------------------------------------------------
    L.append(
        "  ! Lightweight C-compatible struct for global_data_type"
    )
    L.append("  ! (generated code only reads: nproma)")
    L.append("  TYPE, BIND(C) :: c_global_data_type")
    L.append("    REAL(c_double) :: divdamp_fac = 0.0_c_double")
    L.append("    REAL(c_double) :: divdamp_fac_o2 = 0.0_c_double")
    L.append("    REAL(c_double) :: iau_wgt_dyn = 0.0_c_double")
    L.append("    INTEGER(c_int) :: divdamp_order = 0")
    L.append("    INTEGER(c_int) :: divdamp_type = 0")
    L.append("    INTEGER(c_int) :: grf_intmethod_e = 0")
    L.append("    INTEGER(c_int) :: i_am_accel_node = 0")
    L.append("    INTEGER(c_int) :: iadv_rhotheta = 0")
    L.append("    INTEGER(c_int) :: igradp_method = 0")
    L.append("    INTEGER(c_int) :: is_iau_active = 0")
    L.append("    INTEGER(c_int) :: itime_scheme = 0")
    L.append("    INTEGER(c_int) :: l_limited_area = 0")
    L.append("    INTEGER(c_int) :: ldeepatmo_field = 0")
    L.append("    INTEGER(c_int) :: lextra_diffu = 0")
    L.append("    INTEGER(c_int) :: lvert_nest = 0")
    L.append("    INTEGER(c_int) :: nproma = 0")
    L.append("    INTEGER(c_int) :: rayleigh_type = 0")
    L.append("    INTEGER(c_int) :: timer_intp = 0")
    L.append("    INTEGER(c_int) :: timer_solve_nh_cellcomp = 0")
    L.append("    INTEGER(c_int) :: timer_solve_nh_edgecomp = 0")
    L.append("    INTEGER(c_int) :: timer_solve_nh_veltend = 0")
    L.append("    INTEGER(c_int) :: timer_solve_nh_vimpl = 0")
    L.append("    INTEGER(c_int) :: timer_solve_nh_vnupd = 0")
    L.append("    INTEGER(c_int) :: timers_level = 0")
    L.append("    TYPE(c_ptr) :: kstart_dd3d = c_null_ptr")
    L.append("    TYPE(c_ptr) :: kstart_moist = c_null_ptr")
    L.append("    TYPE(c_ptr) :: ndyn_substeps_var = c_null_ptr")
    L.append("    TYPE(c_ptr) :: nflat_gradp = c_null_ptr")
    L.append("    TYPE(c_ptr) :: nflatlev_ptr = c_null_ptr")
    L.append("    TYPE(c_ptr) :: nrdmax_ptr = c_null_ptr")
    L.append("  END TYPE c_global_data_type")
    L.append("")

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
    L.append(
        "    ! Placeholder — actual init happens lazily on first call per variant."
    )
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

    # ---- velocity_tendencies_gpu ----
    L.append(
        "  SUBROUTINE velocity_tendencies_gpu(p_prog, p_patch, p_int, "
        "p_metrics, p_diag, &"
    )
    L.append(
        "      z_w_concorr_me, z_kin_hor_e, z_vt_ie, ntnd, istep, lvn_only, &"
    )
    L.append("      dtime, dt_linintp_ubc, ldeepatmo)")
    if serde:
        L.append(
            "    USE vt_serde, ONLY: vt_tic, do_serialize, "
            "dycore_generation, vt_generation, at, serialize, "
            "serialize_global_data"
        )
    L.append("    USE mo_exception, ONLY: message, message_text")
    L.append("    USE mo_mpi, ONLY: i_am_accel_node")
    L.append("    TYPE(t_nh_prog), INTENT(INOUT), TARGET :: p_prog")
    L.append("    TYPE(t_patch), INTENT(IN), TARGET :: p_patch")
    L.append("    TYPE(t_int_state), INTENT(IN), TARGET :: p_int")
    L.append("    TYPE(t_nh_metrics), INTENT(INOUT), TARGET :: p_metrics")
    L.append("    TYPE(t_nh_diag), INTENT(INOUT), TARGET :: p_diag")
    L.append(
        "    REAL(vp), DIMENSION(:,:,:), INTENT(INOUT), TARGET "
        ":: z_w_concorr_me, z_kin_hor_e, z_vt_ie"
    )
    L.append("    INTEGER, INTENT(IN) :: ntnd, istep")
    L.append("    LOGICAL, INTENT(IN) :: lvn_only, ldeepatmo")
    L.append("    REAL(wp), INTENT(IN) :: dtime, dt_linintp_ubc")
    L.append("")
    L.append("    ! C-compatible structs")
    L.append("    TYPE(c_global_data_type), TARGET :: g_global")
    L.append("    TYPE(glue_t_patch), TARGET :: g_patch")
    L.append("    TYPE(glue_t_int_state), TARGET :: g_int")
    L.append("    TYPE(glue_t_nh_prog), TARGET :: g_prog")
    L.append("    TYPE(glue_t_nh_metrics), TARGET :: g_metrics")
    L.append("    TYPE(glue_t_nh_diag), TARGET :: g_diag")
    L.append("")
    L.append(
        "    ! Host helper arrays for nflatlev / nrdmax (SAVE — allocated once)"
    )
    L.append(
        "    INTEGER(c_int), ALLOCATABLE, TARGET, SAVE :: a_nflatlev(:), a_nrdmax(:)"
    )
    L.append("")
    L.append("    ! Scalar conversions")
    L.append("    INTEGER(c_int) :: c_istep, c_ntnd, c_lvn_only, c_ldeepatmo")
    L.append("    REAL(c_double) :: c_dtime, c_dt_linintp_ubc")
    L.append("    REAL(c_double) :: c_max_vcfl_dyn")
    L.append("")
    L.append("    ! First-call flag: constant structs marshal once (glue copy gating)")
    L.append("    LOGICAL, SAVE :: vt_first_call = .TRUE.")
    L.append("")
    # Hook: before call
    L.append("    ! --- START INSTRUMENTATION ---")
    L.append("    ! NOTE: If you modify this instrumentation, please update "
             "velocity_tendencies_gpu in wrapper.f90.")
    if serde:
        L.append("    CALL vt_tic()")
        L.append("    IF (do_serialize .AND. dycore_generation == 1 .AND. vt_generation == 1) THEN")
        L.append("#ifdef _OPENACC")
        L.append("      WRITE (message_text, *) 'D2H ', vt_generation")
        L.append("      CALL message('', message_text)")
        L.append("      !$ACC UPDATE HOST(p_prog%vn, p_prog%w) &")
        L.append("      !$ACC&  HOST(p_diag%vn_ie, p_diag%vt, p_diag%w_concorr_c, &")
        L.append("      !$ACC&       p_diag%ddt_vn_apc_pc, p_diag%ddt_w_adv_pc, &")
        L.append("      !$ACC&       p_diag%max_vcfl_dyn) &")
        L.append("      !$ACC&  HOST(p_metrics%coeff_gradekin, p_metrics%coeff1_dwdz, &")
        L.append("      !$ACC&       p_metrics%coeff2_dwdz, p_metrics%wgtfac_c, &")
        L.append("      !$ACC&       p_metrics%wgtfac_e, p_metrics%wgtfacq_e, &")
        L.append("      !$ACC&       p_metrics%ddqz_z_half, p_metrics%ddqz_z_full_e, &")
        L.append("      !$ACC&       p_metrics%ddxn_z_full, p_metrics%ddxt_z_full, &")
        L.append("      !$ACC&       p_metrics%deepatmo_gradh_mc, p_metrics%deepatmo_invr_mc, &")
        L.append("      !$ACC&       p_metrics%deepatmo_gradh_ifc, p_metrics%deepatmo_invr_ifc) &")
        L.append("      !$ACC&  HOST(p_int%c_lin_e, p_int%e_bln_c_s, &")
        L.append("      !$ACC&       p_int%geofac_rot, p_int%geofac_grdiv, &")
        L.append("      !$ACC&       p_int%geofac_n2s, p_int%rbf_vec_coeff_e, &")
        L.append("      !$ACC&       p_int%cells_aw_verts) &")
        L.append("      !$ACC&  HOST(p_patch%edges%tangent_orientation, &")
        L.append("      !$ACC&       p_patch%edges%inv_dual_edge_length, &")
        L.append("      !$ACC&       p_patch%edges%inv_primal_edge_length, &")
        L.append("      !$ACC&       p_patch%edges%area_edge, p_patch%edges%f_e, &")
        L.append("      !$ACC&       p_patch%edges%cell_idx, p_patch%edges%cell_blk, &")
        L.append("      !$ACC&       p_patch%edges%vertex_idx, p_patch%edges%vertex_blk, &")
        L.append("      !$ACC&       p_patch%edges%quad_idx, p_patch%edges%quad_blk, &")
        L.append("      !$ACC&       p_patch%cells%edge_idx, p_patch%cells%edge_blk, &")
        L.append("      !$ACC&       p_patch%cells%neighbor_idx, p_patch%cells%neighbor_blk, &")
        L.append("      !$ACC&       p_patch%cells%area, p_patch%cells%f_c, &")
        L.append("      !$ACC&       p_patch%cells%vertex_idx, p_patch%cells%vertex_blk) &")
        L.append("      !$ACC&  HOST(z_w_concorr_me, z_kin_hor_e, z_vt_ie)")
        L.append("      !$ACC WAIT")
        L.append("#endif")
        L.append("")
        L.append("      WRITE (message_text, *) "
                 "'Starting velocity_tendencies for vt_generation ', vt_generation")
        L.append("      CALL message('', message_text)")
        L.append("      CALL serialize(at('p_patch'), p_patch)")
        L.append("      CALL serialize(at('p_int'), p_int)")
        L.append("      CALL serialize(at('ntnd'), ntnd)")
        L.append("      CALL serialize(at('istep'), istep)")
        L.append("      CALL serialize(at('lvn_only'), lvn_only)")
        L.append("      CALL serialize(at('dtime'), dtime)")
        L.append("      CALL serialize(at('dt_linintp_ubc'), dt_linintp_ubc)")
        L.append("      CALL serialize(at('ldeepatmo'), ldeepatmo)")
        L.append("      CALL serialize_global_data(at('global_data.t0'))")
        L.append("      CALL serialize(at('p_prog.t0'), p_prog)")
        L.append("      CALL serialize(at('p_metrics.t0'), p_metrics)")
        L.append("      CALL serialize(at('p_diag.t0'), p_diag)")
        L.append("      CALL serialize(at('z_w_concorr_me.t0'), z_w_concorr_me)")
        L.append("      CALL serialize(at('z_kin_hor_e.t0'), z_kin_hor_e)")
        L.append("      CALL serialize(at('z_vt_ie.t0'), z_vt_ie)")
        L.append("    ENDIF")
        L.append("    ! --- END INSTRUMENTATION ---")
    L.append("")
    L.append("    CALL timer_start(timer_solve_nh_veltend)")
    L.append("")

    # Prepare host helper arrays
    L.append("    ! Host helper arrays (allocated once via SAVE)")
    L.append("    IF (.NOT. ALLOCATED(a_nflatlev)) THEN")
    L.append("      ALLOCATE(a_nflatlev(SIZE(nflatlev)))")
    L.append("      a_nflatlev = INT(nflatlev, c_int)")
    L.append("    END IF")
    L.append("    IF (.NOT. ALLOCATED(a_nrdmax)) THEN")
    L.append("      ALLOCATE(a_nrdmax(SIZE(nrdmax)))")
    L.append("      a_nrdmax = INT(nrdmax, c_int)")
    L.append("    END IF")
    L.append("")

    # Fill lightweight structs
    L.append("    ! Fill C-compatible structs")
    L.append("    g_global%nproma = INT(nproma, c_int)")
    L.append("")
    L.append("    ! Pack Fortran structs into C-compatible glue types.")
    L.append("    ! Constant structs (patch/int/metrics) marshal once (vt_first_call);")
    L.append("    ! mutable inputs (prog/diag) re-copy every call.")
    L.append("    CALL ctor(p_patch, g_patch, vt_first_call)")
    L.append("    CALL ctor(p_int, g_int, vt_first_call)")
    L.append("    CALL ctor(p_prog, g_prog, .TRUE.)")
    L.append("    CALL ctor(p_metrics, g_metrics, vt_first_call)")
    L.append("    CALL ctor(p_diag, g_diag, .TRUE.)")
    L.append("    vt_first_call = .FALSE.")
    L.append("")

    # Convert scalars
    L.append("    ! Convert scalars")
    L.append("    c_istep = INT(istep, c_int)")
    L.append("    c_ntnd = INT(ntnd, c_int)")
    L.append("    c_lvn_only = MERGE(1_c_int, 0_c_int, lvn_only)")
    L.append("    c_ldeepatmo = MERGE(1_c_int, 0_c_int, ldeepatmo)")
    L.append("    c_dtime = REAL(dtime, c_double)")
    L.append("    c_dt_linintp_ubc = REAL(dt_linintp_ubc, c_double)")
    L.append("    c_max_vcfl_dyn = REAL(p_diag%max_vcfl_dyn, c_double)")
    L.append("")

    # Collect superset of GPU device-pointer fields across all variants
    gpu_fields: list[str] = []
    gpu_fields_seen: set[str] = set()
    for v in VARIANTS:
        for p in all_params[v]:
            name = p["name"]
            if name.startswith("gpu___CG_"):
                field = _cg_to_fortran_field(name)
            elif name.startswith("z_") and p["is_pointer"]:
                field = name[: -len("_fp64")] if name.endswith("_fp64") else name
            else:
                continue
            if field not in gpu_fields_seen:
                gpu_fields_seen.add(field)
                gpu_fields.append(field)

    # Hook: debug prints — host pointers (before HOST_DATA)
    if debug_prints:
        L.append("    ! >>> HOOK: debug_prints (host) <<<")
        L.append("    WRITE(0, '(A)') '=== VT GPU HOST POINTERS ==='")
        for field in gpu_fields:
            if field.startswith("z_"):
                ones = ",".join(["1"] * 3)
                L.append(
                    f"    WRITE(0, '(A,Z16)') '  {field} = ', "
                    f"TRANSFER(c_loc({field}({ones})), 1_c_intptr_t)"
                )
            elif "%" in field:
                L.append(
                    f"    WRITE(0, '(A,Z16)') '  {field} = ', "
                    f"TRANSFER(c_loc({field}), 1_c_intptr_t)"
                )
        L.append("    WRITE(0, '(A)') '=== END HOST POINTERS ==='")
        L.append("")

    # Emit !$ACC HOST_DATA USE_DEVICE(...) block
    # Inside this block, c_loc() returns the GPU device pointer.
    L.append("    !$ACC HOST_DATA USE_DEVICE( &")
    for i, field in enumerate(gpu_fields):
        comma = ", &" if i < len(gpu_fields) - 1 else " &"
        L.append(f"    !$ACC   {field}{comma}")
    L.append("    !$ACC )")
    L.append("")

    # Hook: debug prints — device pointers (inside HOST_DATA)
    if debug_prints:
        L.append("    ! >>> HOOK: debug_prints (device, inside HOST_DATA) <<<")
        L.append("    WRITE(0, '(A)') '=== VT GPU DEVICE POINTERS (inside HOST_DATA) ==='")
        for field in gpu_fields:
            if field.startswith("z_"):
                ones = ",".join(["1"] * 3)
                L.append(
                    f"    WRITE(0, '(A,Z16)') '  HD {field} = ', "
                    f"TRANSFER(c_loc({field}({ones})), 1_c_intptr_t)"
                )
            elif "%" in field:
                L.append(
                    f"    WRITE(0, '(A,Z16)') '  HD {field} = ', "
                    f"TRANSFER(c_loc({field}), 1_c_intptr_t)"
                )
        L.append("    WRITE(0, '(A)') '=== END DEVICE POINTERS ==='")
        L.append("")

    # Dispatch per variant
    for i, v in enumerate(VARIANTS):
        params = all_params[v]
        s = v.replace("velocity_no_nproma_if_prop_", "")
        m = re.search(r"lvn_only_(\d+)_istep_(\d+)", v)
        assert m is not None
        lvn_val, istep_val = m.group(1), m.group(2)
        lvn_bool = ".FALSE." if lvn_val == "0" else ".TRUE."
        prefix = "IF" if i == 0 else "ELSE IF"

        L.append(
            f"    {prefix} ((lvn_only .EQV. {lvn_bool}) "
            f".AND. (istep == {istep_val})) THEN"
        )
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
    L.append("    !$ACC END HOST_DATA")
    L.append("")

    # Copy scalar output back
    L.append("    ! Copy scalar output back to Fortran type")
    L.append("    p_diag%max_vcfl_dyn = c_max_vcfl_dyn")
    L.append("")
    L.append("    CALL timer_stop(timer_solve_nh_veltend)")
    L.append("")

    L.append("")

    L.append("  END SUBROUTINE velocity_tendencies_gpu")
    L.append("")
    L.append("END MODULE vt_wrapper")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate Fortran wrapper for DaCe velocity_tendencies GPU library."
    )
    parser.add_argument("--stage-dir", default=STAGE_DIR_DEFAULT)
    parser.add_argument("--output", default=OUTPUT_DEFAULT)
    parser.add_argument("--serde", action="store_true",
                        help="Include serialization instrumentation hooks")
    parser.add_argument("--debug-prints", action="store_true",
                        help="Include GPU pointer debug prints")
    args = parser.parse_args()

    all_params: dict[str, list[dict]] = {}
    for v in VARIANTS:
        header_path = Path(args.stage_dir) / f"{v}.h"
        header_text = header_path.read_text()
        params = parse_init_params(header_text, v)
        all_params[v] = params
        print(f"Parsed {v}: {len(params)} parameters")

    fortran = generate(all_params, serde=args.serde, debug_prints=args.debug_prints)
    Path(args.output).write_text(fortran)
    flags = []
    if args.serde:
        flags.append("serde")
    if args.debug_prints:
        flags.append("debug-prints")
    flag_str = f" [{', '.join(flags)}]" if flags else ""
    print(f"Generated {args.output} ({len(fortran.splitlines())} lines){flag_str}")


if __name__ == "__main__":
    main()
