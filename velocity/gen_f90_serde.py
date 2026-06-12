#!/usr/bin/env python3
"""
Fortran 90 Serialization Generator (Consolidated)

This script generates a standalone Fortran 90 module for serializing data
structures preserved in a DaCe SDFG. It integrates logic from gen_serde.py
and its associated wrapper, focused exclusively on Fortran output.

Key features:
- Serialization of intrinsic types, arrays, and derived types.
- Precision-preserving real number serialization (e28.20).
- Global data serialization across modules.
- F90-C glue code generation for DaCe interoperability.
- Constant evaluation propagation for static analysis.
- Metadata serialization (rank, shape, bounds) for deserializer context.
- Strict data format preservation.
"""

import re
import argparse
from pathlib import Path
from dataclasses import dataclass
from itertools import chain, combinations
from typing import Generator, Dict, Tuple, List, Optional, Union, Any

# FParser imports for AST manipulation
from fparser.api import get_reader
from fparser.two.Fortran2003 import (
    Module,
    Derived_Type_Stmt,
    Module_Subprogram_Part,
    Data_Component_Def_Stmt,
    Procedure_Stmt,
    Interface_Block,
    Program,
    Intrinsic_Type_Spec,
    Dimension_Component_Attr_Spec,
    Declaration_Type_Spec,
    Private_Components_Stmt,
    Component_Part,
    Derived_Type_Def,
    Subroutine_Subprogram,
    Subroutine_Stmt,
    Main_Program,
    Function_Subprogram,
    Use_Stmt,
    Name,
    Only_List,
    Rename,
    Generic_Spec,
    Specification_Part,
    Entity_Decl,
)
from fparser.two.utils import walk

# DaCe imports
import dace
from dace import SDFG
from dace.frontend.fortran.ast_desugaring import (
    identifier_specs,
    append_children,
    set_children,
    SPEC_TABLE,
    SPEC,
    find_name_of_node,
    alias_specs,
    remove_self,
    find_scope_spec,
    GLOBAL_DATA_OBJ_NAME,
    GLOBAL_DATA_TYPE_NAME,
    find_type_of_entity,
    const_eval_nodes,
)
from dace.frontend.fortran.ast_utils import singular, children_of_type, atmost_one
from dace.frontend.fortran.fortran_parser import ParseConfig, create_fparser_ast

# Constants
NEW_LINE = "NEW_LINE('A')"

STUBS = """
module mpi
  integer, parameter :: mpi_success = 0
end module mpi
module netcdf_nf_interfaces
end module netcdf_nf_interfaces
module netcdf4_nf_interfaces
end module netcdf4_nf_interfaces
module netcdf_nf_data
  integer, parameter :: nf_nowrite = 0, nf_global = 0, nf_noerr = 0, nf_strerror = 1
  integer, parameter :: nfint2 = 2
  interface nf_open
  end interface nf_open
  interface nf_close
  end interface nf_close
end module netcdf_nf_data
"""

BUILTINS = """
module iso_c_binding
  integer, parameter :: c_int8_t = 1, c_int16_t = 2, c_int32_t = 4, c_int64_t = 8
  integer, parameter :: c_char = c_int8_t, c_signed_char = c_char, c_bool = c_int8_t, c_int = c_int32_t, c_long = c_int, c_size_t = c_int64_t
  integer, parameter :: c_float = 4, c_double = 8
  type c_ptr
  end type c_ptr
  type c_funptr
  end type c_funptr
  type(c_ptr), parameter :: c_null_ptr = c_ptr()
  character(kind=c_char), parameter :: c_null_char = char(0)
  interface c_f_pointer
    module procedure :: cfp_logical_r3
  end interface c_f_pointer
  interface c_f_procpointer
  end interface c_f_procpointer
  interface c_loc
  end interface c_loc
  interface c_associated
    module procedure :: cass_cptr
  end interface c_associated
contains
  subroutine cfp_logical_r3(cptr, fptr, shape, lower)
    type(c_ptr), intent(in) :: cptr
    logical, pointer, intent(out) :: fptr(:, :, :)
    integer, optional :: shape(:)
    integer, optional :: lower(:)
  end subroutine cfp_logical_r3
  logical function cass_cptr(a, b)
    type(c_ptr), intent(in) :: a
    type(c_ptr), optional, intent(in) :: b
  end function cass_cptr
end module iso_c_binding
module iso_fortran_env
  integer, parameter :: real32 = 4
  integer, parameter :: real64 = 8
  integer, parameter :: int32 = 4
  integer, parameter :: int64 = 8
  character, parameter :: compiler_version = "", compiler_options = ""
end module iso_fortran_env
"""


@dataclass(frozen=True)
class DerivedTypeInfo:
    name: str
    tdef: Derived_Type_Def
    spec: SPEC


@dataclass(frozen=True)
class ComponentInfo:
    name: str
    type: Union[Intrinsic_Type_Spec, Declaration_Type_Spec]
    ptr: bool
    alloc: bool
    rank: int
    shape: Any


def find_all_f90_files(root: Path) -> Generator[Path, None, None]:
    """Recursively finds all Fortran source files."""
    if root.is_file():
        yield root
    else:
        for f in chain(root.rglob("*.f90"), root.rglob("*.F90"), root.rglob("*.incf")):
            yield f


def gen_f90_serde_module_skeleton(mod_name: str = "serde") -> Module:
    """Generates the base Fortran module with core serialization infrastructure."""
    return Module(
        get_reader(f"""
module {mod_name}
  implicit none
  interface serialize
    module procedure :: W_string
  end interface serialize
  integer :: generation = 0
contains
  subroutine tic()
    generation = generation + 1
  end subroutine tic
  function cat(prefix, asis) result(path)
    character(len=*), intent(in) :: prefix
    character(len=:), allocatable :: path
    character(len=50) :: gen
    logical, intent(in) :: asis
    if (asis) then
      path = prefix
    else
      write (gen, '(g0)') generation
      path = prefix // '.' // trim(gen) // ".data"
    endif
  end function cat
  function at(prefix, asis) result(io)
    character(len=*), intent(in) :: prefix
    integer :: io
    logical, optional, intent(in) :: asis
    logical :: asis_local
    asis_local = .false.
    if (present(asis)) asis_local = asis
    open (NEWUNIT=io, FILE=cat(prefix, asis_local), STATUS="replace", ACTION="write")
  end function at
  subroutine W_string(io, x, cleanup, nline)
    integer :: io
    character(len=*), intent(in) :: x
    integer :: i, xend
    logical, optional, intent(in) :: cleanup, nline
    logical :: cleanup_local, nline_local
    cleanup_local = .true. ; nline_local = .true.
    if (present(cleanup)) cleanup_local = cleanup
    if (present(nline)) nline_local = nline
    xend = len(x)
    do i = 1, len(x)
      if (x(i:i) == char(0)) then
        xend = i - 1
        exit
      end if
    end do
    write (io, '(A)', advance='no') trim(x(1:xend))
    if (nline_local)  write (io, '(g0)', advance='no') {NEW_LINE}
    if (cleanup_local) close(UNIT=io)
  end subroutine W_string
end module {mod_name}
""")
    )


def gen_base_type_serializer(
    typ: str, kind: Optional[int] = None
) -> Subroutine_Subprogram:
    """Generates a serializer for an intrinsic Fortran type."""
    assert typ in {"logical", "integer", "real"}
    fn_name = f"W_{typ}{kind or ''}"
    kind_str = f"(kind={kind})" if kind else ""
    if typ == "logical":
        op = "y = merge(1, 0, x)\n  write (io, '(g0)', advance='no') y"
    elif typ == "real":
        op = "write (buf, '(e28.20)') x; write (io, '(A)', advance='no') trim(adjustl(buf))"
    else:
        op = "write (io, '(g0)', advance='no') x"
    return Subroutine_Subprogram(
        get_reader(f"""
subroutine {fn_name}(io, x, cleanup, nline)
  character(len=50) :: buf
  integer :: io
  {typ}{kind_str}, intent(in) :: x
  integer :: y
  logical, optional, intent(in) :: cleanup, nline
  logical :: cleanup_local, nline_local
  cleanup_local = .true. ; nline_local = .true.
  if (present(cleanup)) cleanup_local = cleanup
  if (present(nline)) nline_local = nline
  {op}
  if (nline_local)  write (io, '(g0)', advance='no') {NEW_LINE}
  if (cleanup_local) close(UNIT=io)
end subroutine {fn_name}
""")
    )


def generate_array_meta_f90(arr: str, rank: int) -> List[str]:
    """Generates code to serialize array metadata."""
    return f"""
call serialize(io, "# rank", cleanup=.false.)
call serialize(io, {rank}, cleanup=.false.)
call serialize(io, "# size", cleanup=.false.)
do kmeta = 1, {rank}
  call serialize(io, size({arr}, kmeta), cleanup=.false.)
end do
call serialize(io, "# lbound", cleanup=.false.)
do kmeta = 1, {rank}
  call serialize(io, lbound({arr}, kmeta), cleanup=.false.)
end do
""".strip().split("\n")


def generate_pointer_meta_f90(
    ptr: str, rank: int, candidates: Dict[str, Tuple]
) -> List[str]:
    """Generates code to track pointer association within a derived type."""
    cand_checks = []
    for c, c_shape in candidates.items():
        c_rank = len(c_shape)
        assert rank <= c_rank
        for subsc in combinations(range(c_rank), c_rank - rank):
            ops, subsc_str, subsc_ser = [], [], []
            for k in range(c_rank):
                if k not in subsc:
                    subsc_str.append(":")
                    subsc_ser.append(
                        'call serialize(io, ":", cleanup=.false., nline=.false.)'
                    )
                else:
                    subsc_str.append(f"kmeta_{k}")
                    subsc_ser.append(
                        f"call serialize(io, kmeta_{k}, cleanup=.false., nline=.false.)"
                    )
                    ops.append(
                        f"do kmeta_{k} = lbound({c}, {k + 1}), ubound({c}, {k + 1})"
                    )

            subsc_str_joined = ", ".join(subsc_str)
            subsc_ser_joined = (
                '\n call serialize(io, ",", cleanup=.false., nline=.false.) \n'.join(
                    subsc_ser
                )
            )

            check_block = f"""
if (associated({ptr}, {c}({subsc_str_joined}))) then
  kmeta = 1
  call serialize(io, "=> {c}(", cleanup=.false., nline=.false.)
  {subsc_ser_joined}
  call serialize(io, "))", cleanup=.false.)
end if
"""
            cand_checks.append("\n".join(ops + [check_block] + ["end do"] * len(ops)))

    cand_checks_str = "\n".join(cand_checks)
    return f"""
if (associated({ptr})) then
  kmeta = 0
  {cand_checks_str}
  call serialize(io, "# missing", cleanup=.false.)
  call serialize(io, (kmeta == 0), cleanup=.false.)
  call serialize(io, {ptr}, cleanup=.false.)
end if
""".strip().split("\n")


def generate_array_serializer_f90(
    dtyp: str, rank: int, tag: str, use: Optional[str] = None
) -> Subroutine_Subprogram:
    """Generates a type-and-rank specific array serializer."""
    iter_vars = ", ".join([f"k{k}" for k in range(1, rank + 1)])
    decls = f"{dtyp}, intent(in) :: x({', '.join([':'] * rank)})\ninteger :: k, kmeta, {iter_vars}"

    loop_ops = []
    for k in reversed(range(1, rank + 1)):
        loop_ops.append(f"do k{k} = lbound(x, {k}), ubound(x, {k})")
    loop_ops.append(f"call serialize(io, x({iter_vars}), cleanup=.false.)")
    loop_ops.extend(["end do"] * rank)

    meta_ops = generate_array_meta_f90("x", rank)
    meta_ops_str = "\n    ".join(meta_ops)
    loop_ops_str = "\n  ".join(loop_ops)
    fn_name = f"W_{tag}_R_{rank}"
    return Subroutine_Subprogram(
        get_reader(f"""
subroutine {fn_name}(io, x, cleanup, nline, meta)
  {use or ""}
  integer :: io
  {decls}
  logical, optional, intent(in) :: cleanup, nline, meta
  logical :: cleanup_local, nline_local, meta_local
  cleanup_local = .true. ; nline_local = .true. ; meta_local = .true.
  if (present(cleanup)) cleanup_local = cleanup
  if (present(nline)) nline_local = nline
  if (present(meta)) meta_local = meta
  if (meta_local) then
    {meta_ops_str}
  endif
  call serialize(io, "# entries", cleanup=.false.)
  {loop_ops_str}
  if (cleanup_local) close(UNIT=io)
end subroutine {fn_name}
""")
    )


def iterate_over_derived_types(
    ident_map: SPEC_TABLE,
) -> Generator[DerivedTypeInfo, None, None]:
    for tspec, dt in ident_map.items():
        if isinstance(dt, Derived_Type_Stmt):
            yield DerivedTypeInfo(f"{dt.children[1]}", dt.parent, tspec)


def iterate_over_public_components(
    dt: DerivedTypeInfo,
) -> Generator[ComponentInfo, None, None]:
    private = False
    for c in dt.tdef.children:
        if isinstance(c, Private_Components_Stmt):
            private = True
        if not isinstance(c, Component_Part) or private:
            continue
        for cdef in walk(c, Data_Component_Def_Stmt):
            ctyp, attrs, cdecls = cdef.children
            ptr = "POINTER" in f"{attrs}" if attrs else False
            alloc = "ALLOCATABLE" in f"{attrs}" if attrs else False
            dims = (
                atmost_one(
                    a
                    for a in attrs.children
                    if isinstance(a, Dimension_Component_Attr_Spec)
                )
                if attrs
                else None
            )
            if dims:
                _, dims = dims.children
            for cdecl in cdecls.children:
                cname, shape, _, _ = cdecl.children
                if not shape:
                    shape = dims
                rank = len(shape.children) if shape else 0
                if isinstance(ctyp, Intrinsic_Type_Spec):
                    dtyp, kind = ctyp.children
                    dtyp = f"{dtyp}"
                    if dtyp == "DOUBLE PRECISION":
                        dtyp, kind = "REAL", "(KIND = 8)"
                    if kind is None:
                        kind = {"INTEGER": "(KIND = 4)", "REAL": "(KIND = 4)"}.get(dtyp)
                    ctyp = Intrinsic_Type_Spec(f"{dtyp}{kind or ''}")
                yield ComponentInfo(f"{cname}", ctyp, ptr, alloc, rank, shape)


def _keep_only_derived_types(ast: Program) -> Program:
    for x in reversed(
        walk(
            ast,
            (Main_Program, Interface_Block, Subroutine_Subprogram, Function_Subprogram),
        )
    ):
        remove_self(x)
    ident_map = identifier_specs(ast)
    aliases = set(ident_map.keys())
    for olist in walk(ast, Only_List):
        use = olist.parent
        mod_name = singular(children_of_type(use, Name)).string
        scope_spec = find_scope_spec(use)
        for c in olist.children:
            if isinstance(c, (Name, Generic_Spec)):
                src, tgt = c, c
            else:
                _, src, tgt = c.children
            src_spec, tgt_spec = scope_spec + (f"{src}",), (mod_name, f"{tgt}")
            if tgt_spec in aliases:
                aliases.add(src_spec)
            else:
                remove_self(c)
    return ast


def _get_sdfg_structs(g: SDFG) -> Dict[str, Dict[str, str]]:
    structs = {
        v.name: v for v in g.arrays.values() if isinstance(v, dace.data.Structure)
    }
    structs.update(
        {
            v.stype.name: v.stype
            for v in g.arrays.values()
            if isinstance(v, dace.data.ContainerArray)
            and isinstance(v.stype, dace.data.Structure)
        }
    )
    while True:
        new = {
            m.name: m
            for v in structs.values()
            for m in v.members.values()
            if isinstance(m, dace.data.Structure) and m.name not in structs
        }
        new.update(
            {
                m.stype.name: m.stype
                for v in structs.values()
                for m in v.members.values()
                if isinstance(m, dace.data.ContainerArray)
                and isinstance(m.stype, dace.data.Structure)
                and m.stype.name not in structs
            }
        )
        if not new:
            break
        structs.update(new)

    def _ctype(v):
        if isinstance(v, dace.data.Scalar):
            return f"{v.ctype}"
        return f"{v.ctype}*" if isinstance(v, dace.data.Array) else f"{v.ctype}"

    return {
        k: {kk: _ctype(vv) for kk, vv in v.members.items()} for k, v in structs.items()
    }


def _parse_shared_struct_defs(
    header_path: str = "include/shared_struct_defs.h",
) -> Tuple[Dict[str, List[Tuple[str, str]]], Dict[str, Dict[str, str]]]:
    """Parse shared_struct_defs.h to get the full field list for each struct.

    Returns:
        (struct_fields, union_aliases)
        struct_fields: {struct_name: [(field_name, c_type), ...]} in declaration order.
            Union blocks are collapsed to a single field (the first variant).
        union_aliases: {struct_name: {alias_name: canonical_name}} mapping all
            union member names to the first (canonical) member name.
    """
    header = Path(header_path)
    if not header.exists():
        return {}, {}

    text = header.read_text()
    # Remove comments
    text = re.sub(r"//[^\n]*", "", text)

    result = {}
    all_aliases = {}

    # Extract struct bodies using brace-counting (handles nested {} in initializers)
    for m in re.finditer(r"struct\s+(\w+)\s*\{", text):
        sname = m.group(1)
        start = m.end()
        depth = 1
        pos = start
        while pos < len(text) and depth > 0:
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
            pos += 1
        body = text[start : pos - 1]

        # Extract union aliases before collapsing
        aliases = {}
        for um in re.finditer(r"union\s*\{(.*?)\}", body, flags=re.DOTALL):
            inner = um.group(1)
            members = re.findall(r"(\w+)\s*(?:\*{0,2})\s+(\w+)\s*;", inner)
            if members:
                canonical = members[0][1]  # first member name
                for _, mname in members:
                    aliases[mname] = canonical
        all_aliases[sname] = aliases

        # Collapse union blocks to first member
        def replace_union(um):
            inner = um.group(1)
            fm = re.search(r"(\w+)\s*(\*{0,2})\s+(\w+)\s*;", inner)
            if fm:
                ptr = fm.group(2)
                return f"{fm.group(1)} {ptr}{fm.group(3)} = {{{{}}}};"
            return ""

        body = re.sub(r"union\s*\{(.*?)\}", replace_union, body, flags=re.DOTALL)

        fields = []
        for line in body.split(";"):
            # Strip and remove any brace initializers
            line = re.sub(r"\{[^}]*\}", "", line).strip()
            if not line:
                continue
            # Match: type [*[*]] name [= ...]
            fm = re.match(r"(\w+)\s*(\*{0,2})\s*(\w+)\s*(?:=.*)?$", line)
            if fm:
                ctype = fm.group(1)
                stars = fm.group(2)
                fname = fm.group(3)
                if stars:
                    ctype = ctype + stars
                fields.append((fname, ctype))

        result[sname] = fields

    return result, all_aliases


def _generate_f90_c_glue_code(
    ast: Program, sdfg_structs: Dict[str, Dict[str, str]], mod_name: str
) -> Module:
    # Parse the full C struct layouts from the header
    full_c_structs, union_aliases = _parse_shared_struct_defs()

    glue_uses, ident_map = [], identifier_specs(ast)
    for dt in iterate_over_derived_types(ident_map):
        if dt.name not in sdfg_structs:
            continue
        glue_uses.append(f"use {dt.spec[0]}, only: {dt.name}")
        for z in iterate_over_public_components(dt):
            if (
                z.name in sdfg_structs[dt.name]
                and z.rank
                and isinstance(z.type, Declaration_Type_Spec)
            ):
                ctyp = f"{z.type.children[1]}"
                mod_results = [
                    k[0] for k in ident_map.keys() if len(k) == 2 and k[-1] == ctyp
                ]
                if mod_results:
                    glue_uses.append(f"use {mod_results[0]}, only: {ctyp}")

    def f90_type(tx):
        return {
            "int": "INTEGER(c_int)",
            "float": "REAL(c_float)",
            "double": "REAL(c_double)",
            "uint8_t": "INTEGER(c_int8_t)",
        }.get(tx, "TYPE(c_ptr)" if tx.endswith("*") else f"TYPE({tx})")

    # Generate struct defs using full C layout when available
    struct_def_parts = []
    for n, v in sdfg_structs.items():
        if n in full_c_structs:
            # Use the full C struct field order and types
            lines = []
            for fname, ctype in full_c_structs[n]:
                lines.append(f"  {f90_type(ctype)} :: m_{fname}")
            struct_def_parts.append(
                f"type, bind(C) :: glue_{n}\n"
                + "\n".join(lines)
                + f"\nend type glue_{n}"
            )
        else:
            # Fallback: SDFG-only fields (sorted)
            struct_def_parts.append(
                f"type, bind(C) :: glue_{n}\n"
                + "\n".join([f"  {f90_type(t)} :: m_{c}" for c, t in sorted(v.items())])
                + f"\nend type glue_{n}"
            )
    f90_struct_defs = "\n".join(struct_def_parts)

    def glue_logic(tx, nx, dtname):
        if nx.startswith("__f2dace_"):
            return "", ""
        if tx in {"int", "float", "double"}:
            return f"out % m_{nx} = inp % {nx}", ""
        if tx.endswith("*"):
            # Only reference dimension fields that exist in the C struct.
            # Remap SDFG field names through union aliases to canonical names.
            c_fields_for_dt = {fname for fname, _ in full_c_structs.get(dtname, [])}
            aliases_for_dt = union_aliases.get(dtname, {})
            all_known_dt = c_fields_for_dt | set(aliases_for_dt.keys())
            active_sdfg = (
                sdfg_structs[dtname]
                if not c_fields_for_dt
                else {
                    k: v for k, v in sdfg_structs[dtname].items() if k in all_known_dt
                }
            )
            # Remap to canonical names
            active_fields = {}
            for k, v in active_sdfg.items():
                canonical = aliases_for_dt.get(k, k)
                active_fields[canonical] = v
            sas = {
                re.sub(r"__f2dace_SA_(.*?)_d_(\d+)_s_.*", r"\1 \2", k): k
                for k in active_fields
                if k.startswith("__f2dace_SA_")
            }
            soas = {
                re.sub(r"__f2dace_SOA_(.*?)_d_(\d+)_s_.*", r"\1 \2", k): k
                for k in active_fields
                if k.startswith("__f2dace_SOA_")
            }
            basetx = tx.removesuffix("*")

            if basetx.endswith("*"):
                basetx = basetx.removesuffix("*")
                assert basetx in sdfg_structs
                dims = [k.split()[1] for k in sas if k.split()[0] == nx]
                assert len(dims) == 3  # We assume 3D only for now
                idx = ",".join([":" for _ in dims])
                size_call = ",".join(
                    [f"size(inp % {nx}, {int(d) + 1})" for d in sorted(dims)]
                )
                decl = f"type(glue_{basetx}), allocatable, target, save :: a_{nx}({idx})\n  type({basetx}), pointer :: pt_{nx}\n  integer :: i_{nx}, j_{nx}, k_{nx}"
                init = f"if (initalloc .and. .not. allocated(a_{nx})) allocate(a_{nx}({size_call}))\n  do i_{nx}=lbound(a_{nx}, 1), ubound(a_{nx}, 1)\n    do j_{nx}=lbound(a_{nx}, 2), ubound(a_{nx}, 2)\n      do k_{nx}=lbound(a_{nx}, 3), ubound(a_{nx}, 3)\n        if (initalloc .and. .not. associated(pt_{nx})) allocate(pt_{nx})\n        call ctor(inp % {nx}(i_{nx}, j_{nx}, k_{nx}), a_{nx}(i_{nx}, j_{nx}, k_{nx}), initalloc)\n      end do\n    end do\n  end do\n  out % m_{nx} = c_loc(a_{nx})"
            elif basetx in sdfg_structs:
                decl = f"type(glue_{basetx}), allocatable, target, save :: a_{nx}"
                init = f"if (initalloc .and. .not. allocated(a_{nx})) allocate(a_{nx})\n  call ctor(inp % {nx}, a_{nx}, initalloc) ; out % m_{nx} = c_loc(a_{nx})"
            else:
                dims = [k.split()[1] for k in sas if k.split()[0] == nx]
                idx = ",".join([":" for _ in dims])
                size_call = ",".join(
                    [f"size(inp % {nx}, {int(d) + 1})" for d in sorted(dims)]
                )
                decl = f"{f90_type(basetx)}, allocatable, target, save :: a_{nx}({idx})"
                init = f"if (initalloc .and. .not. allocated(a_{nx})) allocate(a_{nx}({size_call}))\n  if (initalloc) a_{nx} = inp % {nx}\n  out % m_{nx} = c_loc(a_{nx})"

            for k, v in sas.items():
                dim_idx = int(k.split()[1]) + 1
                if k.split()[0] == nx:
                    init += f"\n  out % m_{v} = size(inp % {nx}, {dim_idx})"
            for k, v in soas.items():
                dim_idx = int(k.split()[1]) + 1
                if k.split()[0] == nx:
                    init += f"\n  out % m_{v} = lbound(inp % {nx}, {dim_idx})"
            return init, decl
        return "", ""

    ctors = []
    for n, v in sdfg_structs.items():
        if n == "global_data_type":
            continue

        # Only generate ctor logic for SDFG fields that exist in the C struct.
        # Fields in the SDFG but not in the C struct are flattened from nested
        # sub-structs and are handled by recursive ctor calls.
        # Union aliases: SDFG may reference any union member name — map to canonical.
        c_field_names = {fname for fname, _ in full_c_structs.get(n, [])}
        aliases_for_n = union_aliases.get(n, {})
        # A field is "in the C struct" if it's a direct field OR a union alias
        all_known = c_field_names | set(aliases_for_n.keys())
        v_filtered = (
            {c: t for c, t in v.items() if c in all_known} if c_field_names else v
        )
        # Remap aliased field names to their canonical (first union member) names
        v_remapped = {}
        for c, t in v_filtered.items():
            canonical = aliases_for_n.get(c, c)
            v_remapped[canonical] = t
        ops = [glue_logic(t, c, n) for c, t in sorted(v_remapped.items())]

        # Zero-init fields from the full C struct that are NOT in the SDFG.
        zero_inits = []
        if n in full_c_structs:
            for fname, ctype in full_c_structs[n]:
                if fname not in v_remapped:
                    ft = f90_type(ctype)
                    if "INTEGER" in ft:
                        zero_inits.append(f"out % m_{fname} = 0")
                    elif "REAL" in ft:
                        zero_inits.append(f"out % m_{fname} = 0.0")
                    elif "c_ptr" in ft:
                        zero_inits.append(f"out % m_{fname} = c_null_ptr")

        ctors.append(
            f"subroutine ctor_{n}(inp, out, initalloc)\n  type({n}), intent(in) :: inp\n  type(glue_{n}), intent(inout) :: out\n  logical, intent(in) :: initalloc\n  "
            + "\n  ".join([x[1] for x in ops if x[1]])
            + "\n  "
            + "\n  ".join(zero_inits)
            + "\n  "
            + "\n  ".join([x[0] for x in ops if x[0]])
            + f"\nend subroutine ctor_{n}"
        )

    ifcs = "\n  ".join(
        [
            f"module procedure :: ctor_{n}"
            for n in sdfg_structs
            if n != "global_data_type"
        ]
    )
    return Module(
        get_reader(f"""
module f90_glue_{mod_name}
  use, intrinsic :: iso_c_binding
  {"\n  ".join(sorted(list(set(glue_uses))))}
  implicit none
  {f90_struct_defs}
  interface ctor
    {ifcs}
  end interface ctor
contains
  {"\n".join(ctors)}
end module f90_glue_{mod_name}
""")
    )


def generate_f90_serialization_code(
    ast: Program, g: SDFG, mod_name: str = "serde"
) -> str:
    ast = _keep_only_derived_types(ast)
    sdfg_structs = _get_sdfg_structs(g)
    f90_mod = gen_f90_serde_module_skeleton(mod_name)
    impls = singular(sp for sp in walk(f90_mod, Module_Subprogram_Part))
    proc_names = ["W_string"]

    base_serializers = [
        gen_base_type_serializer("logical"),
        *[gen_base_type_serializer("integer", k) for k in [1, 2, 4, 8]],
        *[gen_base_type_serializer("real", k) for k in [4, 8]],
    ]
    for fn in base_serializers:
        proc_names.append(
            f"{singular(children_of_type(fn, Subroutine_Stmt)).children[1]}"
        )
        append_children(impls, fn)

    ident_map, array_serializers = identifier_specs(ast), {}
    for dt in iterate_over_derived_types(ident_map):
        if dt.name not in sdfg_structs or dt.name == GLOBAL_DATA_TYPE_NAME:
            continue
        f90_ser_ops, array_map = [], {}
        for z in iterate_over_public_components(dt):
            if not z.ptr and z.rank > 0:
                array_map[(f"{z.type}", z.rank)] = (z.name, z.shape)

        for z in iterate_over_public_components(dt):
            if z.name not in sdfg_structs[dt.name]:
                continue

            # Protocol match for t_patch: add nblks fields to the beginning
            if dt.name == "t_patch" and z.name == "cells":
                for blk in ["nblks_c", "nblks_e", "nblks_v"]:
                    f90_ser_ops.append(
                        f"call serialize(io , '# {blk}', cleanup=.false.)\n  call serialize(io , x % {blk}, cleanup=.false.)"
                    )

            f90_ser_ops.append(f"call serialize(io , '# {z.name}', cleanup=.false.)")

            # Ensure array serializer exists
            if z.rank > 0:
                tag = (
                    f"{z.type}".replace("TYPE(", "dt_")
                    .replace("(KIND =", "_")
                    .replace(" ", "_")
                    .replace(")", "")
                    .lower()
                )
                if (tag, z.rank) not in array_serializers:
                    ctyp_name = f"{z.type.children[1]}"
                    mod_results = [
                        k[0]
                        for k in ident_map.keys()
                        if len(k) == 2 and k[-1] == ctyp_name
                    ]
                    use_stmt = (
                        f"use {mod_results[0]}, only: {ctyp_name}"
                        if mod_results and isinstance(z.type, Declaration_Type_Spec)
                        else None
                    )
                    array_serializers[(tag, z.rank)] = generate_array_serializer_f90(
                        f"{z.type}", z.rank, tag, use_stmt
                    )

            if z.ptr:
                f90_ser_ops.append(
                    f"call serialize(io, '# assoc', cleanup=.false.)\n  call serialize(io, associated(x%{z.name}), cleanup=.false.)"
                )
                candidates = {
                    f"x%{v[0]}": v[1].children
                    for k, v in array_map.items()
                    if k[0] == f"{z.type}" and z.rank <= k[1]
                }
                f90_ser_ops.extend(
                    generate_pointer_meta_f90(f"x%{z.name}", z.rank, candidates)
                )
            else:
                if z.alloc or z.rank > 0:
                    alloc_check = f"allocated(x%{z.name})" if z.alloc else ".true."
                    f90_ser_ops.append(
                        f"call serialize(io, '# alloc', cleanup=.false.)\n  call serialize(io, {alloc_check}, cleanup=.false.)"
                    )
                    if z.alloc:
                        f90_ser_ops.append(f"if (allocated(x%{z.name})) then")

                if z.rank > 0:
                    f90_ser_ops.extend(generate_array_meta_f90(f"x%{z.name}", z.rank))
                    f90_ser_ops.append(
                        f"call serialize(io, x%{z.name}, cleanup=.false., nline=.true., meta=.false.)"
                    )
                else:
                    f90_ser_ops.append(
                        f"call serialize(io, x%{z.name}, cleanup=.false.)"
                    )

                if z.alloc:
                    f90_ser_ops.append("end if")

        dt_sub = Subroutine_Subprogram(
            get_reader(f"""
subroutine W_{dt.name}(io, x, cleanup, nline)
  use {dt.spec[0]}, only: {dt.name}
  integer :: io
  type({dt.name}), target, intent(in) :: x
  logical, optional, intent(in) :: cleanup, nline
  integer :: kmeta, kmeta_0, kmeta_1, kmeta_2, kmeta_3, kmeta_4, kmeta_5, kmeta_6, kmeta_7, kmeta_8, kmeta_9
  logical :: cleanup_local, nline_local
  cleanup_local = .true. ; nline_local = .true.
  if (present(cleanup)) cleanup_local = cleanup
  if (present(nline)) nline_local = nline
  {"\n  ".join(f90_ser_ops)}
  if (nline_local)  write (io, '(g0)', advance='no') {NEW_LINE}
  if (cleanup_local) close(UNIT=io)
end subroutine W_{dt.name}
""")
        )
        proc_names.append(f"W_{dt.name}")
        append_children(impls, dt_sub)

    for fn in array_serializers.values():
        proc_names.append(
            f"{singular(children_of_type(fn, Subroutine_Stmt)).children[1]}"
        )
        append_children(impls, fn)

    # ICON Module Remapping Table
    MOD_MAP = {
        "lextra_diffu": "mo_nonhydrostatic_config",
        "nproma": "mo_parallel_config",
        "timers_level": "mo_run_config",
        "nrdmax": "mo_vertical_grid",
        "nflatlev": "mo_init_vgrid",
        "timer_solve_nh_veltend": "mo_timer",
        "timer_intp": "mo_timer",
        "i_am_accel_node": "mo_mpi",
    }

    gdata_uses, gdata_ops, gdata_seen = [], [], set()
    if GLOBAL_DATA_OBJ_NAME in g.arrays:
        gdata = g.arrays[GLOBAL_DATA_OBJ_NAME].members

        # Mapping of original names to their renamed versions in the AST
        renamed_map = {}
        for spart in walk(ast, Specification_Part):
            for var in walk(spart, Entity_Decl):
                vname = find_name_of_node(var)
                match = re.match(r"^([a-zA-Z0-9_]+)_var_[0-9]+$", vname)
                if match:
                    renamed_map[match.group(1)] = vname
                else:
                    renamed_map[vname] = vname

        for og_name in gdata.keys():
            if og_name in gdata_seen:
                continue
            vname = renamed_map.get(og_name, og_name)

            # Find the module containing this variable
            target_mod = MOD_MAP.get(og_name)
            if not target_mod:
                for mod in walk(ast, Module):
                    if any(
                        find_name_of_node(v) == vname for v in walk(mod, Entity_Decl)
                    ):
                        target_mod = find_name_of_node(mod)
                        break

            if target_mod:
                gdata_uses.append(f"use {target_mod}, only : {vname} => {og_name}")
                gdata_ops.append(
                    f"call serialize(io, '# {og_name}', cleanup=.false.)\n  call serialize(io, {vname}, cleanup=.false.)"
                )
                gdata_seen.add(og_name)

    append_children(
        impls,
        Subroutine_Subprogram(
            get_reader(f"""
subroutine serialize_global_data(io)
  {"\n  ".join(sorted(list(set(gdata_uses))))}
  integer :: io
  {"\n  ".join(gdata_ops)}
  close(UNIT=io)
end subroutine serialize_global_data
""")
        ),
    )

    # Update the 'serialize' interface with all gathered procedure names
    proc_names_stmt = Procedure_Stmt(
        f"module procedure {', '.join(sorted(list(set(proc_names))))}"
    )
    iface = singular(
        p for p in walk(f90_mod, Interface_Block) if find_name_of_node(p) == "serialize"
    )
    set_children(iface, (iface.children[0], proc_names_stmt, iface.children[2]))

    # Final cleanup: Remove all synthetic 'global_mod' uses from the module and subroutines
    f90_mod_str = f90_mod.tofortran()
    f90_mod_str = re.sub(r"USE global_mod, ONLY: global_data_type\n", "", f90_mod_str)
    f90_mod_str = re.sub(
        r"TYPE\(global_data_type\)", "integer", f90_mod_str
    )  # Stub out the type decl if it remains

    glue_mod = _generate_f90_c_glue_code(ast, sdfg_structs, mod_name)
    glue_mod_str = glue_mod.tofortran()
    glue_mod_str = re.sub(r"USE global_mod, ONLY: global_data_type\n", "", glue_mod_str)

    return f"{glue_mod_str}\n{f90_mod_str}"


def main():
    parser = argparse.ArgumentParser(
        description="Generate Fortran serialization code (F90 only)."
    )
    parser.add_argument(
        "-i",
        "--in_src",
        type=str,
        required=True,
        action="append",
        help="Input Fortran directories/files.",
    )
    parser.add_argument(
        "-g", "--in_sdfg", type=str, required=True, help="Input SDFG file."
    )
    parser.add_argument("-f", "--out_f90", type=str, help="Output F90 file.")
    parser.add_argument(
        "-m", "--module_name", type=str, default="serde", help="Serde module name."
    )
    args = parser.parse_args()

    input_f90s = [
        f for p in [Path(x) for x in args.in_src] for f in find_all_f90_files(p)
    ]
    cfg = ParseConfig(sources=input_f90s)
    cfg.sources["_stubs.f90"] = STUBS
    cfg.sources["_builtins.f90"] = BUILTINS
    ast = const_eval_nodes(_keep_only_derived_types(create_fparser_ast(cfg)))

    f90_code = generate_f90_serialization_code(
        ast, SDFG.from_file(args.in_sdfg), args.module_name
    )
    if args.out_f90:
        with open(args.out_f90, "w") as f:
            f.write(f90_code)
    else:
        print(f90_code)


if __name__ == "__main__":
    main()
