#!/usr/bin/env python3
"""Profile SASS/PTX instruction mix per kernel.

Usage:
    # SASS (preferred — shows actual machine instructions)
    python utils/sass_profile.py ptx_out/all_kernels.sass

    # PTX (fallback — shows virtual ISA)
    python utils/sass_profile.py ptx_out/velocity_no_nproma_if_prop_lvn_only_0_istep_1_cuda.ptx

    # Compare two builds side-by-side
    python utils/sass_profile.py --compare fp64.sass fp32.sass
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# SASS classification (sm_90 / Hopper)
# ---------------------------------------------------------------------------
# Prefix-based: SASS mnemonics start with a letter group that determines type.

_SASS_FP64 = {"DADD", "DMUL", "DFMA", "DSET", "DSETP", "DMNMX", "DMMA", "DSUB", "MUFU"}
_SASS_FP32 = {"FADD", "FMUL", "FFMA", "FSET", "FSETP", "FMNMX", "FSWZADD", "FSUB", "FCMP"}
_SASS_FP16 = {"HADD", "HMUL", "HFMA", "HSET", "HSETP", "HMNMX", "HMMA", "HADD2", "HMUL2", "HFMA2", "HSUB"}
_SASS_INT = {
    "IADD", "IADD3", "IMAD", "IMUL", "ISET", "ISETP", "IMNMX",
    "LEA", "LOP3", "SHL", "SHR", "SHF", "BFE", "BFI", "FLO", "POPC",
    "PRMT", "SEL", "SGXT", "BMSK",
}
_SASS_LDST = {
    "LDG", "STG", "LDS", "STS", "LDL", "STL", "LDC", "LDGSTS",
    "LDGDEPBAR", "ATOM", "ATOMS", "RED",
}
_SASS_CONV = {"F2F", "F2I", "I2F", "I2I", "FRND"}
_SASS_CTRL = {"BRA", "BRX", "JMX", "CALL", "RET", "EXIT", "BREAK", "CONT", "SSY", "SYNC", "CAL", "JCAL"}
_SASS_MOVE = {"MOV", "MOV32I", "SHFL", "PRMT", "S2R", "CS2R", "R2P", "P2R"}
_SASS_MISC = {"NOP", "BAR", "DEPBAR", "YIELD", "NANOSLEEP", "WARPSYNC", "SETCTAID", "VOTE", "MATCH", "REDUX"}
_SASS_MUFU = {"MUFU"}  # transcendentals — could be fp32 or fp64 depending on context
_SASS_TEX = {"TEX", "TLD", "TXQ"}

def _classify_sass(mnemonic: str) -> str:
    """Return category string for a SASS mnemonic."""
    base = mnemonic.split(".")[0]
    if base in _SASS_FP64:
        return "fp64"
    if base in _SASS_FP32:
        return "fp32"
    if base in _SASS_FP16:
        return "fp16"
    if base in _SASS_INT:
        return "int"
    if base in _SASS_LDST:
        return "ld/st"
    if base in _SASS_CONV:
        # Check suffix for precision
        if ".F64" in mnemonic or ".64" in mnemonic:
            return "conv(fp64)"
        if ".F16" in mnemonic or ".16" in mnemonic:
            return "conv(fp16)"
        return "conv(fp32)"
    if base in _SASS_CTRL:
        return "ctrl"
    if base in _SASS_MOVE:
        return "move"
    if base in _SASS_MUFU:
        # MUFU is special function unit — transcendentals, typically fp32
        return "fp32(mufu)"
    if base in _SASS_MISC:
        return "misc"
    if base in _SASS_TEX:
        return "tex"
    return "other"


# ---------------------------------------------------------------------------
# PTX classification
# ---------------------------------------------------------------------------

def _classify_ptx(mnemonic: str) -> str:
    """Return category for a PTX instruction like 'fma.rn.ftz.f32'."""
    if ".f64" in mnemonic:
        return "fp64"
    if ".f32" in mnemonic:
        return "fp32"
    if ".f16" in mnemonic or ".bf16" in mnemonic:
        return "fp16"
    parts = mnemonic.split(".")
    base = parts[0]
    if base in ("ld", "st", "atom", "red", "prefetch"):
        return "ld/st"
    if base in ("bra", "call", "ret", "exit", "bar"):
        return "ctrl"
    if base in ("mov", "shfl"):
        return "move"
    if base in ("cvt", "cvta"):
        return "conv"
    if base in ("setp", "set", "selp", "slct"):
        return "pred"
    # Integer ops
    if any(f".{t}" in mnemonic for t in ("u8", "s8", "u16", "s16", "u32", "s32", "u64", "s64", "b8", "b16", "b32", "b64", "pred")):
        return "int"
    return "other"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# SASS: cuobjdump -sass output
# Lines like:  /*0050*/  @P0 FFMA R4, R2, R3, R4 ;
_SASS_INSN_RE = re.compile(
    r"^\s*/\*[0-9a-fA-F]+\*/\s+"   # address comment
    r"(?:@!?P\d+\s+)?"              # optional predicate
    r"([A-Z][A-Z0-9_.]+)"           # mnemonic (capture group 1)
)

# SASS kernel header:  Fatbin elf code:  or  code for sm_90
_SASS_KERNEL_RE = re.compile(
    r"^\s*(?:\.text\.)?(\S+)\s*:$"   # function label
    r"|Function\s*:\s*(\S+)"         # cuobjdump style
)

# PTX instruction: starts with optional predicate, then mnemonic
_PTX_INSN_RE = re.compile(
    r"^\s+(?:@!?%p\d+\s+)?"   # optional predicate
    r"([a-z][a-z0-9_.]+)"     # mnemonic
    r"\s"                      # followed by space (not a label)
)

_PTX_KERNEL_RE = re.compile(
    r"\.visible\s+\.entry\s+(\S+)"
)


def parse_sass(text: str) -> dict[str, Counter]:
    """Parse cuobjdump -sass output → {kernel_name: Counter(category)}."""
    kernels: dict[str, Counter] = {}
    current: str | None = None

    for line in text.splitlines():
        # Check for kernel/function header
        # cuobjdump format: ".text.KERNEL_NAME:" or "Function : KERNEL_NAME"
        km = _SASS_KERNEL_RE.search(line)
        if km:
            current = km.group(1) or km.group(2)
            if current and current not in kernels:
                kernels[current] = Counter()
            continue

        if current is None:
            continue

        m = _SASS_INSN_RE.match(line)
        if m:
            mnemonic = m.group(1)
            cat = _classify_sass(mnemonic)
            kernels[current][cat] += 1

    return kernels


def parse_ptx(text: str) -> dict[str, Counter]:
    """Parse PTX → {kernel_name: Counter(category)}."""
    kernels: dict[str, Counter] = {}
    current: str | None = None

    for line in text.splitlines():
        km = _PTX_KERNEL_RE.search(line)
        if km:
            current = km.group(1)
            kernels[current] = Counter()
            continue

        if current is None:
            continue

        # End of kernel
        if line.strip() == "}":
            current = None
            continue

        m = _PTX_INSN_RE.match(line)
        if m:
            mnemonic = m.group(1)
            cat = _classify_ptx(mnemonic)
            kernels[current][cat] += 1

    return kernels


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

# Categories in display order
_CATS = ["fp64", "fp32", "fp16", "int", "ld/st", "conv", "conv(fp64)", "conv(fp32)", "conv(fp16)", "ctrl", "move", "pred", "fp32(mufu)", "misc", "tex", "other"]

def _shorten_kernel(name: str, max_len: int = 60) -> str:
    """Shorten kernel name for display."""
    # Strip common DaCe prefixes
    for prefix in ("__dace_runkernel_", "_dace_runkernel_"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if len(name) > max_len:
        name = name[:max_len - 3] + "..."
    return name


def report_single(kernels: dict[str, Counter], *, top_n: int = 0) -> None:
    """Print instruction mix report."""
    # Global totals
    total = Counter()
    for c in kernels.values():
        total += c

    total_insns = sum(total.values())
    if total_insns == 0:
        print("No instructions found.")
        return

    print(f"\n{'='*80}")
    print(f"INSTRUCTION MIX SUMMARY  ({total_insns:,} instructions, {len(kernels)} kernels)")
    print(f"{'='*80}\n")

    # Global breakdown
    print(f"{'Category':<16} {'Count':>10} {'Pct':>8}")
    print(f"{'-'*16} {'-'*10} {'-'*8}")
    for cat in _CATS:
        n = total.get(cat, 0)
        if n > 0:
            print(f"{cat:<16} {n:>10,} {100*n/total_insns:>7.1f}%")
    print(f"{'─'*16} {'─'*10} {'─'*8}")
    print(f"{'TOTAL':<16} {total_insns:>10,} {'100.0%':>8}")

    # FP ratio summary
    fp64_n = total.get("fp64", 0) + total.get("conv(fp64)", 0)
    fp32_n = total.get("fp32", 0) + total.get("fp32(mufu)", 0) + total.get("conv(fp32)", 0)
    fp16_n = total.get("fp16", 0) + total.get("conv(fp16)", 0)
    fp_total = fp64_n + fp32_n + fp16_n
    if fp_total > 0:
        print(f"\n  FP breakdown: fp64={fp64_n:,} ({100*fp64_n/fp_total:.1f}%)  "
              f"fp32={fp32_n:,} ({100*fp32_n/fp_total:.1f}%)  "
              f"fp16={fp16_n:,} ({100*fp16_n/fp_total:.1f}%)")

    # Per-kernel (sorted by total insns, descending)
    if top_n != 0:
        print(f"\n{'='*80}")
        print(f"PER-KERNEL BREAKDOWN (top {top_n if top_n > 0 else 'all'})")
        print(f"{'='*80}\n")

        sorted_kernels = sorted(kernels.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
        if top_n > 0:
            sorted_kernels = sorted_kernels[:top_n]

        for kname, kcounts in sorted_kernels:
            ktotal = sum(kcounts.values())
            if ktotal == 0:
                continue
            short = _shorten_kernel(kname)
            print(f"  {short}")
            print(f"    Total: {ktotal:,}  ", end="")
            parts = []
            for cat in _CATS:
                n = kcounts.get(cat, 0)
                if n > 0:
                    parts.append(f"{cat}={n}({100*n/ktotal:.0f}%)")
            print("  ".join(parts))
            print()


def report_compare(kernels_a: dict[str, Counter], kernels_b: dict[str, Counter],
                   label_a: str, label_b: str) -> None:
    """Print side-by-side comparison of two builds."""
    total_a, total_b = Counter(), Counter()
    for c in kernels_a.values():
        total_a += c
    for c in kernels_b.values():
        total_b += c

    n_a = sum(total_a.values())
    n_b = sum(total_b.values())

    print(f"\n{'='*80}")
    print(f"INSTRUCTION MIX COMPARISON")
    print(f"  A: {label_a}  ({n_a:,} insns, {len(kernels_a)} kernels)")
    print(f"  B: {label_b}  ({n_b:,} insns, {len(kernels_b)} kernels)")
    print(f"{'='*80}\n")

    print(f"{'Category':<16} {'A':>10} {'A%':>7}  {'B':>10} {'B%':>7}  {'Δ':>10}")
    print(f"{'-'*16} {'-'*10} {'-'*7}  {'-'*10} {'-'*7}  {'-'*10}")

    for cat in _CATS:
        a = total_a.get(cat, 0)
        b = total_b.get(cat, 0)
        if a == 0 and b == 0:
            continue
        pct_a = f"{100*a/n_a:.1f}%" if n_a else "-"
        pct_b = f"{100*b/n_b:.1f}%" if n_b else "-"
        delta = b - a
        delta_s = f"{delta:+,}" if delta != 0 else "="
        print(f"{cat:<16} {a:>10,} {pct_a:>7}  {b:>10,} {pct_b:>7}  {delta_s:>10}")

    print(f"{'─'*16} {'─'*10} {'─'*7}  {'─'*10} {'─'*7}  {'─'*10}")
    delta_total = n_b - n_a
    delta_s = f"{delta_total:+,}" if delta_total != 0 else "="
    print(f"{'TOTAL':<16} {n_a:>10,} {'100.0%':>7}  {n_b:>10,} {'100.0%':>7}  {delta_s:>10}")

    # FP ratio comparison
    for label, total, n in [(label_a, total_a, n_a), (label_b, total_b, n_b)]:
        fp64_n = total.get("fp64", 0) + total.get("conv(fp64)", 0)
        fp32_n = total.get("fp32", 0) + total.get("fp32(mufu)", 0) + total.get("conv(fp32)", 0)
        fp16_n = total.get("fp16", 0) + total.get("conv(fp16)", 0)
        fp_total = fp64_n + fp32_n + fp16_n
        if fp_total > 0:
            print(f"\n  {label}: fp64={fp64_n:,} ({100*fp64_n/fp_total:.1f}%)  "
                  f"fp32={fp32_n:,} ({100*fp32_n/fp_total:.1f}%)  "
                  f"fp16={fp16_n:,} ({100*fp16_n/fp_total:.1f}%)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _detect_format(path: Path) -> str:
    """Detect whether file is SASS or PTX."""
    name = path.name.lower()
    if name.endswith(".sass"):
        return "sass"
    if name.endswith(".ptx"):
        return "ptx"
    # Peek at content
    head = path.read_text(errors="replace")[:2000]
    if re.search(r"/\*[0-9a-fA-F]+\*/", head):
        return "sass"
    if ".entry" in head or ".visible" in head:
        return "ptx"
    return "sass"  # default


def _parse_file(path: Path) -> dict[str, Counter]:
    text = path.read_text(errors="replace")
    fmt = _detect_format(path)
    if fmt == "sass":
        return parse_sass(text)
    else:
        return parse_ptx(text)


def main():
    ap = argparse.ArgumentParser(description="Profile SASS/PTX instruction mix")
    ap.add_argument("files", nargs="+", type=Path, help="SASS or PTX files")
    ap.add_argument("--compare", action="store_true",
                    help="Compare two files side-by-side (requires exactly 2 files)")
    ap.add_argument("--top", type=int, default=0,
                    help="Show per-kernel breakdown (0=summary only, -1=all, N=top N)")
    args = ap.parse_args()

    if args.compare:
        if len(args.files) != 2:
            print("--compare requires exactly 2 files", file=sys.stderr)
            sys.exit(1)
        ka = _parse_file(args.files[0])
        kb = _parse_file(args.files[1])
        report_compare(ka, kb, args.files[0].name, args.files[1].name)
    else:
        all_kernels: dict[str, Counter] = {}
        for f in args.files:
            all_kernels.update(_parse_file(f))
        report_single(all_kernels, top_n=args.top)


if __name__ == "__main__":
    main()
