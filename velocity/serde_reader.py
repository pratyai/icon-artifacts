"""Serde format reader → plain Python dicts. No ctypes coupling.

Kept around alongside `run_velocity_sdfg.py` (which now inlines the
dummy-synthesis bits it needs); use this directly if you want to read
serde files without going through the runtime tester.

The on-disk format (text), as written by `generated_data/serde.f90` and
read by `include/serde.h`:

    Scalar file: a single bare value (scientific-notation float, "1"/"0"
                 for booleans, plain integers).

    Array block: # rank
                 <n>
                 # size
                 <sz0> <sz1> … <szN-1>
                 # lbound
                 <lb0> <lb1> … <lbN-1>
                 # entries
                 <v0> <v1> … <v_(prod size - 1)>

    Pointer-to-array prefix (only inside structs):
                 # alloc <flag>                 # for ALLOCATABLE fields
                 (or)
                 # assoc <flag> # missing <flag>  # for POINTER fields
                 # (then array block, or absent if flag != 1)
                 (or, in global_data: nothing — straight to # rank)

    Struct file: a sequence of `# <field-name>` blocks, in the order the
                 corresponding C++ `deserialize(<struct>*, ...)` consumes
                 them. We walk a *schema* (name + kind) to know what to
                 expect at each step.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

# Fortran list-directed output drops the exponent letter once the exponent
# needs three digits, writing 1.0E-112 as "1.0-112". Put the "E" back before
# handing the token to float(). Values that small only survive in formats
# with a wide exponent range, so this shows up in bf16 dumps long before
# fp16 ones, where they have already flushed to zero.
_FORTRAN_EXP = re.compile(r"(?<=[0-9.])([+-])(\d{3,})$")


def parse_fortran_float(tok: str) -> float:
    try:
        return float(tok)
    except ValueError:
        repaired = _FORTRAN_EXP.sub(r"E\1\2", tok)
        return float(repaired)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class _Tokens:
    def __init__(self, text: str):
        self._toks = text.split()
        self._i = 0

    def _peek(self):
        return self._toks[self._i] if self._i < len(self._toks) else None

    def _take(self):
        t = self._toks[self._i]; self._i += 1
        return t

    def expect_header(self, name: str):
        if self._take() != "#":
            raise SyntaxError(f"expected '#' before {name!r}")
        got = self._take()
        if got != name:
            raise SyntaxError(f"expected '# {name}', got '# {got}'")

    def peek_header(self):
        if self._peek() != "#":
            return None
        return self._toks[self._i + 1] if self._i + 1 < len(self._toks) else None

    def take_int(self):
        return int(self._take())

    def take_float(self):
        return parse_fortran_float(self._take())


# ---------------------------------------------------------------------------
# Block readers
# ---------------------------------------------------------------------------

@dataclass
class Array:
    """A serde array: shape (Fortran sizes), Fortran lbounds, F-contiguous data."""
    shape: tuple
    lbound: tuple
    data: np.ndarray


def _read_array_block(t: _Tokens, dtype) -> Array:
    t.expect_header("rank")
    rank = t.take_int()
    t.expect_header("size")
    sizes = tuple(t.take_int() for _ in range(rank))
    t.expect_header("lbound")
    lbs = tuple(t.take_int() for _ in range(rank))
    t.expect_header("entries")
    n = math.prod(sizes) if sizes else 1
    if np.issubdtype(np.dtype(dtype), np.integer):
        flat = np.fromiter((t.take_int() for _ in range(n)), dtype=dtype, count=n)
    else:
        flat = np.fromiter((t.take_float() for _ in range(n)), dtype=dtype, count=n)
    if sizes:
        arr = np.asfortranarray(flat.reshape(sizes, order="F").copy(order="F"))
    else:
        arr = flat
    return Array(sizes, lbs, arr)


def _read_pointer_prefix(t: _Tokens) -> bool:
    """`# alloc <flag>` or `# assoc <flag> # missing <flag>` or (no preamble)."""
    h = t.peek_header()
    if h == "alloc":
        t.expect_header("alloc")
        return t.take_int() == 1
    if h == "assoc":
        t.expect_header("assoc")
        assoc = t.take_int()
        t.expect_header("missing")
        missing = t.take_int()
        return assoc == 1 and missing == 1
    if h == "rank":
        return True  # bare array, no preamble (global_data style)
    raise SyntaxError(f"expected '# alloc'/'# assoc'/'# rank', got '# {h}'")


def _read_struct(t: _Tokens, schema_name: str, schemas: dict) -> dict:
    out: dict = {}
    for fname, kind in schemas[schema_name]:
        t.expect_header(fname)
        out[fname] = _read_field(t, kind, schemas)
    return out


def _read_field(t: _Tokens, kind: str, schemas: dict):
    if kind == "int":
        return t.take_int()
    if kind == "double":
        return t.take_float()
    if kind == "bool":
        return bool(t.take_int())
    if kind in ("ptr_int", "ptr_double"):
        if not _read_pointer_prefix(t):
            return None
        dtype = np.int32 if kind == "ptr_int" else np.float64
        return _read_array_block(t, dtype)
    if kind in ("ptr_int_global", "ptr_double_global"):
        dtype = np.int32 if kind.startswith("ptr_int") else np.float64
        return _read_array_block(t, dtype)
    if kind.startswith("struct:"):
        return _read_struct(t, kind.split(":", 1)[1], schemas)
    raise ValueError(f"unknown field kind {kind!r}")


# ---------------------------------------------------------------------------
# Public file-level API
# ---------------------------------------------------------------------------

def load_struct(path: str, schema_name: str, schemas: dict) -> dict:
    """Read a struct serde file. Returns a nested dict matching the schema."""
    with open(path) as f:
        return _read_struct(_Tokens(f.read()), schema_name, schemas)


def load_array(path: str, dtype=np.float64) -> Array:
    """Read a top-level array serde file."""
    with open(path) as f:
        return _read_array_block(_Tokens(f.read()), dtype)


def load_scalar(path: str, kind: str = "double"):
    """Read a top-level scalar serde file. `kind` ∈ {int, double, bool}."""
    with open(path) as f:
        text = f.read().strip()
    if kind == "int":
        return int(text)
    if kind == "bool":
        return text.strip() in ("1", "true", "True", ".true.", ".TRUE.")
    return parse_fortran_float(text)
