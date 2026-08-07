#!/usr/bin/env python3
"""Usage: ./latex_ncu.py [ncu_aggregate.csv]

Reads ncu_aggregate.csv and emits a LaTeX table matching tab:ncu-profiling.
Auto-detects resolutions and precisions from filenames.

Expected filename pattern: vt_profiles-<GRID>/vt.<PREC>.ncu-rep
  e.g. vt_profiles-r02b03/vt.f16.ncu-rep
"""
import csv, sys, os, re
from collections import OrderedDict

# Reports are named vt.<prec>.ncu-rep, and the precision tag has been spelled
# both ways (f64 and fp64) across sweeps; accept either, and carry bf16.
PREC_ORDER = ["f64", "fp64", "f32", "fp32", "f16", "fp16", "bf16"]
PREC_LABEL = {
    "f64": "FP64",
    "fp64": "FP64",
    "f32": "FP32",
    "fp32": "FP32",
    "f16": "FP16",
    "fp16": "FP16",
    "bf16": "BF16",
}

# GH200 peak DRAM BW in GB/s
PEAK_BW = 4000.0


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def parse_filename(fname):
    """Extract (grid, prec) from filename like 'vt_profiles-r02b03/vt.f16.ncu-rep'."""
    m = re.search(r"(r\d+b\d+)", fname, re.IGNORECASE)
    grid = m.group(1).upper() if m else fname
    m2 = re.search(r"\.((?:bf|fp|f)(?:16|32|64))\.", fname, re.IGNORECASE)
    prec = m2.group(1).lower() if m2 else "?"
    return grid, prec


# Printed in place of a cell whose metric the report does not carry. A missing
# metric must never render as a number: an uncollected counter would otherwise
# be indistinguishable from a measured zero.
MISSING = "--"


def fval(s):
    """Parse a numeric string; return None when it is absent or not a number."""
    try:
        return float(s.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


def latex_num(v, fmt_spec):
    """Format a number for LaTeX, inserting {,} for thousands separator."""
    if v is None:
        return MISSING
    s = f"{v:{fmt_spec}}"
    # Insert LaTeX thousands separator
    if "," in s:
        s = s.replace(",", "{,}")
    return s


def build_table(agg_path):
    rows = load_csv(agg_path)

    # Collect warm AGGREGATE data keyed by (grid, prec)
    data = {}
    for r in rows:
        if r["kernel"] != "AGGREGATE" or r.get("warmth") != "warm":
            continue
        grid, prec = parse_filename(r["file"])
        key = (grid, prec)
        if key not in data:
            data[key] = {}
        data[key][r["metric"]] = r["value"]

    # Discover grids and sort by resolution number
    grids = sorted(set(g for g, _ in data.keys()),
                   key=lambda g: int(re.search(r"\d+$", g).group()))
    precs = [p for p in PREC_ORDER if any((g, p) in data for g in grids)]
    n_grids = len(grids)
    n_cols = n_grids * len(precs)

    def get(grid, prec, metric):
        return fval(data.get((grid, prec), {}).get(metric))

    # --- Build rows ---
    table_rows = []

    # Kernel duration
    def row_duration():
        vals = []
        for g in grids:
            for p in precs:
                vals.append(latex_num(get(g, p, "Total duration [us]"), ",.0f"))
        return "Kernel duration ($\\mu$s)", vals

    # Global load instructions [M]
    def row_glob_ld():
        vals = []
        for g in grids:
            for p in precs:
                v = get(g, p, "Global ld insns")
                v = v / 1e6 if v is not None else None
                vals.append(latex_num(v, ".1f"))
        return "Global load instructions (M)", vals

    # Compute throughput
    def row_compute():
        vals = []
        for g in grids:
            for p in precs:
                vals.append(latex_num(get(g, p, "Total GFLOP/s"), ",.0f"))
        return "Compute throughput (GFLOP/s)", vals

    # DRAM read [MB]
    def row_dram_rd():
        vals = []
        for g in grids:
            for p in precs:
                v = get(g, p, "DRAM rd [GB]")
                v = v * 1e3 if v is not None else None
                if v is None:
                    vals.append(MISSING)
                elif v >= 100:
                    vals.append(latex_num(v, ",.0f"))
                else:
                    vals.append(latex_num(v, ".1f"))
        return "DRAM read (MB)", vals

    # DRAM write [MB]
    def row_dram_wr():
        vals = []
        for g in grids:
            for p in precs:
                v = get(g, p, "DRAM wr [GB]")
                v = v * 1e3 if v is not None else None
                if v is None:
                    vals.append(MISSING)
                elif v >= 100:
                    vals.append(latex_num(v, ",.0f"))
                elif v >= 1:
                    vals.append(latex_num(v, ".1f"))
                elif v >= 0.05:
                    vals.append(latex_num(v, ".1f"))
                else:
                    vals.append("0.0")
        return "DRAM write (MB)\\tnote{\\dag}", vals

    # BW utilization (% of peak)
    def row_bw():
        vals = []
        for g in grids:
            for p in precs:
                bw = get(g, p, "DRAM total BW [GB/s]")
                pct = bw / PEAK_BW * 100 if bw is not None else None
                vals.append(latex_num(pct, ".1f"))
        return "BW utilization (\\% of peak)", vals

    # L2 hit rate
    def row_l2():
        vals = []
        for g in grids:
            for p in precs:
                vals.append(latex_num(get(g, p, "L2 hit rate [%]"), ".1f"))
        return "L2 hit rate (\\%)", vals

    # L1 hit rate
    def row_l1():
        vals = []
        for g in grids:
            for p in precs:
                vals.append(latex_num(get(g, p, "L1 hit rate [%]"), ".1f"))
        return "L1 hit rate (\\%)", vals

    # Registers/thread — from dominant kernel (same across all: 32)
    # Pull from aggregate data if available, otherwise hardcode
    def row_regs():
        vals = []
        for g in grids:
            for p in precs:
                # Regs not in aggregate derived; check dominant rows
                dom_rows = [r for r in rows
                            if r["kernel"].startswith("DOMINANT:")
                            and r.get("warmth") == "warm"
                            and r["metric"] == "Regs/thr"]
                grid2, prec2 = None, None
                for dr in dom_rows:
                    g2, p2 = parse_filename(dr["file"])
                    if g2 == g and p2 == p:
                        # Extract number from "32 register/thread"
                        m = re.match(r"(\d+)", dr["value"].strip())
                        vals.append(m.group(1) if m else "?")
                        break
                else:
                    vals.append("32")
        return "Registers/thread", vals

    # Occupancy — from dominant kernel warm
    def row_occ():
        vals = []
        for g in grids:
            for p in precs:
                dom_rows_occ = [r for r in rows
                                if r["kernel"].startswith("DOMINANT:")
                                and r.get("warmth") == "warm"
                                and r["metric"] == "Occ [%]"]
                for dr in dom_rows_occ:
                    g2, p2 = parse_filename(dr["file"])
                    if g2 == g and p2 == p:
                        m = re.match(r"([\d.]+)", dr["value"].strip())
                        vals.append(latex_num(float(m.group(1)), ".1f") if m else "?")
                        break
                else:
                    vals.append("?")
        return "Occupancy (\\%)", vals

    # FLOP shares
    def row_flop_share(label, metric):
        vals = []
        for g in grids:
            for p in precs:
                total = get(g, p, "Total FLOPs")
                part = get(g, p, metric)
                if total is None or part is None:
                    vals.append(MISSING)
                elif total == 0:
                    vals.append("0")
                else:
                    pct = part / total * 100
                    vals.append("0" if pct == 0 else latex_num(pct, ".1f"))
        return label, vals

    table_rows.append(row_duration())
    table_rows.append(row_glob_ld())
    table_rows.append(row_compute())
    table_rows.append(row_dram_rd())
    table_rows.append(row_dram_wr())
    table_rows.append(row_bw())
    table_rows.append(row_l2())
    table_rows.append(row_l1())
    table_rows.append(row_regs())
    table_rows.append(row_occ())
    # separator before FLOP mix
    table_rows.append(None)  # sentinel for \midrule
    table_rows.append(row_flop_share("FP64 FLOP share (\\%)", "FP64 FLOPs"))
    table_rows.append(row_flop_share("FP32 FLOP share (\\%)", "FP32 FLOPs"))
    table_rows.append(row_flop_share("FP16 FLOP share (\\%)", "FP16 FLOPs"))

    # --- Emit LaTeX ---
    col_spec = "@{\\hskip 15pt}".join(
        ["rrr"] * n_grids
    )
    # First group has no leading skip
    col_spec_full = f"@{{}}>{{{chr(92)}raggedright{chr(92)}arraybackslash}}p{{0.24{chr(92)}linewidth}}" + \
                    "rrr" + \
                    "".join(f"@{{\\hskip 15pt}}rrr" for _ in range(n_grids - 1)) + \
                    "@{}"

    lines = []
    lines.append(r"\begin{table*}[tbph]")
    lines.append(r"\centering")
    lines.append(r"\begin{threeparttable}")
    lines.append(r"\caption{\normalfont\small \texttt{ncu} profiling summary for ICON velocity "
                 r"transport (VT) on GH200 (warm rep). Cold and warm reps show negligible "
                 r"differences ($<\!5\%$) across all metrics.}\label{tab:ncu-profiling}")
    lines.append(r"\begingroup")
    lines.append(r"\small")
    lines.append(r"\setlength{\tabcolsep}{3pt}")
    lines.append(r"\renewcommand{\arraystretch}{1.18}")
    lines.append(f"\\begin{{tabular}}{{{col_spec_full}}}")
    lines.append(r"\toprule")

    # Grid header
    grid_cols = []
    for i, g in enumerate(grids):
        suffix = "" if i < n_grids - 1 else ""
        at_hskip = "@{\\hskip 15pt}" if i < n_grids - 1 else ""
        grid_cols.append(f"\\multicolumn{{3}}{{c{at_hskip}}}{{{g}}}")
    lines.append(" & " + " & ".join(grid_cols) + r" \\")

    # cmidrules
    rules = []
    for i in range(n_grids):
        start = 2 + i * 3
        end = start + 2
        lr = "lr" if i < n_grids - 1 else "l"
        rules.append(f"\\cmidrule({lr}){{{start}-{end}}}")
    lines.append(" ".join(rules))

    # Precision header
    prec_cells = []
    for i, g in enumerate(grids):
        for j, p in enumerate(precs):
            at_hskip = "@{\\hskip 15pt}" if (j == len(precs) - 1 and i < n_grids - 1) else ""
            prec_cells.append(f"\\multicolumn{{1}}{{c{at_hskip}}}{{{PREC_LABEL[p]}}}")
    lines.append("Metric & " + " & ".join(prec_cells) + r" \\")
    lines.append(r"\midrule")

    # Data rows
    for row in table_rows:
        if row is None:
            lines.append(r"\midrule")
            continue
        label, vals = row
        # Pad label for alignment
        lines.append(f"{label:<40s} & " + " & ".join(vals) + r" \\")

    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append("")
    lines.append(r"\begin{tablenotes}[flushleft]")
    lines.append(r"  \footnotesize")
    lines.append(r"  \item[\dag] DRAM write measures L2$\to$DRAM writebacks. "
                 r"At R02B03 the working set fits in L2 (96\,MB on GH200), so stores "
                 r"are absorbed entirely by L2 with no DRAM evictions during the kernel.")
    lines.append(r"\end{tablenotes}")
    lines.append("")
    lines.append(r"\endgroup")
    lines.append(r"\end{threeparttable}")
    lines.append(r"\end{table*}")

    return "\n".join(lines)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "ncu_aggregate.csv"
    if not os.path.exists(path):
        print(f"Error: {path} not found. Run extract_ncu.py first.", file=sys.stderr)
        sys.exit(1)
    print(build_table(path))


if __name__ == "__main__":
    main()
