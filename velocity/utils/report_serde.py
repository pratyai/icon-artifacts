"""Report on all_comparisons.db — detailed tables for cross and convergence comparisons.

Usage:
    python utils/report_serde.py all_comparisons.db
    python utils/report_serde.py all_comparisons.db --latex          # LaTeX snippets (MIN across phys)
    python utils/report_serde.py all_comparisons.db --phys 2         # SNR at a specific phys gen (e.g. paper's first step)
    python utils/report_serde.py all_comparisons.db --phys 2 --latex # paper-shaped LaTeX snippet
"""

import argparse
import sqlite3
from typing import Optional

import polars as pl


def load_all(db_path: str) -> pl.DataFrame:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT grid, tag, phys, field, sub_field, status, "
        "max_abs, max_rel, mae, rmse, SNR_db, vSNR_db, "
        "mismatches, total_elements "
        "FROM comparisons ORDER BY grid, tag, phys, field, sub_field"
    ).fetchall()
    conn.close()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema=[
        "grid", "tag", "phys", "field", "sub_field", "status",
        "max_abs", "max_rel", "mae", "rmse", "SNR_db", "vSNR_db",
        "mismatches", "total_elements",
    ], orient="row")


def make_name(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.when(pl.col("sub_field") == "-")
        .then(pl.col("field"))
        .otherwise(pl.col("field") + " % " + pl.col("sub_field"))
        .alias("name"),
    )


def print_header(title: str):
    print(f"\n{'=' * 90}")
    print(f"  {title}")
    print("=" * 90)


def print_table(df: pl.DataFrame):
    with pl.Config(tbl_rows=500, tbl_width_chars=240, tbl_cols=-1, float_precision=2):
        print(df)


# ---------------------------------------------------------------------------
# Cross-comparison reports (OG_vs_F32, OG_vs_F16)
# ---------------------------------------------------------------------------

def report_cross(df: pl.DataFrame, grid: str, tag: str):
    """Detailed per-field, per-phys table for one (grid, tag)."""
    sub = df.filter((pl.col("grid") == grid) & (pl.col("tag") == tag))
    if sub.is_empty():
        return
    sub = make_name(sub)

    detail = (
        sub.select(["phys", "name", "status", "SNR_db", "vSNR_db",
                     "max_abs", "max_rel", "rmse", "mismatches", "total_elements"])
        .rename({"SNR_db": "SNR", "vSNR_db": "vSNR"})
        .sort(["name", "phys"])
    )
    print_header(f"CROSS: {grid} / {tag}")
    print_table(detail)


def _cross_tags(df: pl.DataFrame) -> pl.DataFrame:
    """Cross tags = everything NOT starting with 'ss' (convergence prefix)."""
    return df.filter(~pl.col("tag").str.starts_with("ss"))


def report_cross_summary(df: pl.DataFrame, phys: Optional[int] = None):
    """Aggregated table: per (grid, tag, field). If phys is None, min/max across
    phys steps. If phys is set, SNR at that phys gen only (paper convention)."""
    cross = _cross_tags(df)
    if cross.is_empty():
        return
    cross = make_name(cross)

    finite = cross.filter(pl.col("SNR_db").is_finite() & (pl.col("SNR_db") > -900))

    if phys is not None:
        at_phys = finite.filter(pl.col("phys") == phys)
        if at_phys.is_empty():
            print(f"No cross rows at phys={phys}")
            return
        summary = (
            at_phys.select(["grid", "tag", "name",
                            pl.col("SNR_db").round(1).alias("SNR"),
                            pl.col("vSNR_db").round(1).alias("vSNR")])
            .sort(["grid", "tag", "name"])
        )
        print_header(f"CROSS SUMMARY (SNR at phys={phys})")
    else:
        summary = (
            finite.group_by(["grid", "tag", "name"]).agg([
                pl.col("SNR_db").min().round(1).alias("min_SNR"),
                pl.col("SNR_db").max().round(1).alias("max_SNR"),
                pl.col("vSNR_db").min().round(1).alias("min_vSNR"),
                pl.col("vSNR_db").max().round(1).alias("max_vSNR"),
            ])
            .sort(["grid", "tag", "name"])
        )
        print_header("CROSS SUMMARY (min/max SNR across phys steps)")
    print_table(summary)


# ---------------------------------------------------------------------------
# Convergence reports (ss*_vs_ss*)
# ---------------------------------------------------------------------------

def report_convergence(df: pl.DataFrame, grid: str):
    """Detailed convergence for one grid: per-field, per-pair SNR."""
    sub = df.filter(
        (pl.col("grid") == grid) & pl.col("tag").str.starts_with("ss")
    )
    if sub.is_empty():
        return
    sub = make_name(sub)

    # Pick a representative phys step (the earliest with data)
    phys_vals = sorted(sub["phys"].unique().to_list())

    for phys in phys_vals[:3]:  # first 3 phys steps to avoid spam
        at_phys = sub.filter(pl.col("phys") == phys)
        if at_phys.is_empty():
            continue
        detail = (
            at_phys.select(["tag", "name", "SNR_db", "vSNR_db", "mismatches", "total_elements"])
            .rename({"SNR_db": "SNR", "vSNR_db": "vSNR"})
            .sort(["name", "tag"])
        )
        print_header(f"CONVERGENCE: {grid} phys={phys}")
        print_table(detail)

    # Full convergence: worst SNR per (field, pair) across all phys
    finite = sub.filter(pl.col("SNR_db").is_finite() & (pl.col("SNR_db") > -900))
    if finite.is_empty():
        return
    worst = (
        finite.group_by(["tag", "name"]).agg([
            pl.col("SNR_db").min().round(1).alias("worst_SNR"),
            pl.col("SNR_db").max().round(1).alias("best_SNR"),
        ])
        .sort(["name", "tag"])
    )
    print_header(f"CONVERGENCE SUMMARY: {grid} (worst/best SNR across all phys)")
    print_table(worst)


# ---------------------------------------------------------------------------
# LaTeX output
# ---------------------------------------------------------------------------

def _fmt_snr(v) -> str:
    if v is None or v != v:
        return "--"
    if v == float("inf"):
        return r"$\infty$"
    if v == float("-inf"):
        return r"$-\infty$"
    return f"{v:.1f}"


PAPER_FIELDS = ["vn", "w", "vt", "vn_ie", "w_concorr_c"]
PAPER_TAGS = [
    "FP32_vs_FP64",
    "FP16_vs_FP64",
    "FP64_vs_refined",
    "FP32_vs_refined",
    "FP16_vs_refined",
]


def paper_snr_table(df: pl.DataFrame, phys: int):
    """Paper-shaped pivot: one polars table per grid, rows=field, cols=tag,
    values=SNR at the given phys. Limited to paper's 5 fields × 5 tags."""
    cross = _cross_tags(df)
    if cross.is_empty():
        return
    cross = make_name(cross)
    at_phys = cross.filter(
        (pl.col("phys") == phys)
        & pl.col("sub_field").is_in(PAPER_FIELDS)
        & pl.col("tag").is_in(PAPER_TAGS)
        & pl.col("SNR_db").is_finite()
        & (pl.col("SNR_db") > -900)
    )
    if at_phys.is_empty():
        print(f"No rows at phys={phys}")
        return

    field_order = pl.Enum(PAPER_FIELDS)
    tag_order = pl.Enum(PAPER_TAGS)
    for grid in sorted(at_phys["grid"].unique().to_list()):
        sub = at_phys.filter(pl.col("grid") == grid)
        pivot = (
            sub.select([
                pl.col("sub_field").cast(field_order).alias("field"),
                pl.col("tag").cast(tag_order),
                pl.col("SNR_db").round(1),
            ])
            .pivot(values="SNR_db", index="field", on="tag")
            .sort("field")
        )
        print_header(f"PAPER SNR TABLE: grid={grid}, phys={phys}")
        print_table(pivot)


def latex_cross_table(df: pl.DataFrame, phys: Optional[int] = None):
    """LaTeX table: rows=fields, columns=(grid × tag). Aggregation over phys
    steps is MIN unless `phys` is given (then value at that phys gen)."""
    cross = _cross_tags(df)
    if cross.is_empty():
        return
    cross = make_name(cross)
    finite = cross.filter(pl.col("SNR_db").is_finite() & (pl.col("SNR_db") > -900))

    if phys is not None:
        at_phys = finite.filter(pl.col("phys") == phys)
        if at_phys.is_empty():
            return
        summary = (
            at_phys.select(["grid", "tag", "name",
                            pl.col("SNR_db").round(1).alias("SNR")])
            .sort(["name", "grid", "tag"])
        )
        caption = f"SNR at phys={phys} (paper's first-step convention)"
    else:
        summary = (
            finite.group_by(["grid", "tag", "name"]).agg([
                pl.col("SNR_db").min().round(1).alias("SNR"),
            ])
            .sort(["name", "grid", "tag"])
        )
        caption = "min SNR across phys steps (worst-case)"

    grids = sorted(summary["grid"].unique().to_list())
    tags = sorted(summary["tag"].unique().to_list())
    names = sorted(summary["name"].unique().to_list())

    # Build lookup
    lookup = {}
    for row in summary.iter_rows(named=True):
        lookup[(row["name"], row["grid"], row["tag"])] = row["SNR"]

    ncols = len(grids) * len(tags)
    col_spec = "l" + "r" * ncols

    print(f"\n% LaTeX: Cross-comparison SNR ({caption})")
    print(f"\\begin{{tabular}}{{{col_spec}}}")
    print("\\toprule")

    # Header row 1: grid spans
    header1 = [""]
    for g in grids:
        header1.append(f"\\multicolumn{{{len(tags)}}}{{c}}{{{g}}}")
    print(" & ".join(header1) + " \\\\")

    # Header row 2: tags
    header2 = ["Field"]
    for g in grids:
        for t in tags:
            label = t.replace("OG_vs_", "").replace("_", " ")
            header2.append(label)
    print(" & ".join(header2) + " \\\\")
    print("\\midrule")

    for name in names:
        row = [name.replace("_", r"\_").replace("%", r"\%")]
        for g in grids:
            for t in tags:
                val = lookup.get((name, g, t))
                row.append(_fmt_snr(val))
        print(" & ".join(row) + " \\\\")

    print("\\bottomrule")
    print("\\end{tabular}")


def latex_convergence_table(df: pl.DataFrame):
    """LaTeX table: convergence worst-case SNR per grid."""
    conv = df.filter(pl.col("tag").str.starts_with("ss"))
    if conv.is_empty():
        return
    conv = make_name(conv)
    finite = conv.filter(pl.col("SNR_db").is_finite() & (pl.col("SNR_db") > -900))

    summary = (
        finite.group_by(["grid", "tag", "name"]).agg([
            pl.col("SNR_db").min().round(1).alias("worst_SNR"),
        ])
        .sort(["name", "grid", "tag"])
    )

    grids = sorted(summary["grid"].unique().to_list())
    tags = sorted(summary["tag"].unique().to_list())
    names = sorted(summary["name"].unique().to_list())

    lookup = {}
    for row in summary.iter_rows(named=True):
        lookup[(row["name"], row["grid"], row["tag"])] = row["worst_SNR"]

    ncols = len(grids) * len(tags)
    col_spec = "l" + "r" * ncols

    print(f"\n% LaTeX: Convergence SNR (worst across phys steps)")
    print(f"\\begin{{tabular}}{{{col_spec}}}")
    print("\\toprule")

    header1 = [""]
    for g in grids:
        header1.append(f"\\multicolumn{{{len(tags)}}}{{c}}{{{g}}}")
    print(" & ".join(header1) + " \\\\")

    header2 = ["Field"]
    for g in grids:
        for t in tags:
            header2.append(t.replace("_", r"\_"))
    print(" & ".join(header2) + " \\\\")
    print("\\midrule")

    for name in names:
        row = [name.replace("_", r"\_").replace("%", r"\%")]
        for g in grids:
            for t in tags:
                val = lookup.get((name, g, t))
                row.append(_fmt_snr(val))
        print(" & ".join(row) + " \\\\")

    print("\\bottomrule")
    print("\\end{tabular}")


# ---------------------------------------------------------------------------
# DB overview
# ---------------------------------------------------------------------------

def report_overview(df: pl.DataFrame):
    """Quick overview: what's in the DB."""
    grids = sorted(df["grid"].unique().to_list())
    tags = sorted(df["tag"].unique().to_list())
    phys_range = (df["phys"].min(), df["phys"].max())

    cross_tags = [t for t in tags if t.startswith("OG_vs_")]
    conv_tags = [t for t in tags if t.startswith("ss")]

    fields = sorted(df["field"].unique().to_list())

    print_header("DATABASE OVERVIEW")
    print(f"  Grids:        {', '.join(grids)}")
    print(f"  Cross tags:   {', '.join(cross_tags)}")
    print(f"  Conv. tags:   {', '.join(conv_tags)}")
    print(f"  Phys range:   {phys_range[0]}..{phys_range[1]}")
    print(f"  Fields ({len(fields)}): {', '.join(fields[:20])}")
    if len(fields) > 20:
        print(f"                ... and {len(fields) - 20} more")
    print(f"  Total rows:   {len(df)}")

    # Counts per (grid, tag)
    counts = (
        df.group_by(["grid", "tag"]).agg(pl.len().alias("rows"))
        .sort(["grid", "tag"])
    )
    print()
    print_table(counts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Report on all_comparisons.db")
    parser.add_argument("db", help="Path to all_comparisons.db")
    parser.add_argument("--latex", action="store_true", help="Emit LaTeX table snippets")
    parser.add_argument("--phys", type=int, default=None,
                        help="Report SNR at a specific physics_generation "
                             "(paper uses first-step, typically 2). Default: min across all phys.")
    parser.add_argument("--cross-only", action="store_true", help="Only show cross comparisons")
    parser.add_argument("--conv-only", action="store_true", help="Only show convergence")
    args = parser.parse_args()

    df = load_all(args.db)
    if df.is_empty():
        print("Empty database.")
        return

    report_overview(df)

    grids = sorted(df["grid"].unique().to_list())
    all_tags = df["tag"].unique().to_list()
    cross_tags = sorted(t for t in all_tags if not t.startswith("ss"))
    has_conv = any(t.startswith("ss") for t in all_tags)

    # Detailed tables
    if not args.conv_only:
        for grid in grids:
            for tag in cross_tags:
                report_cross(df, grid, tag)
        report_cross_summary(df, phys=args.phys)
        if args.phys is not None:
            paper_snr_table(df, phys=args.phys)

    if not args.cross_only and has_conv:
        for grid in grids:
            report_convergence(df, grid)

    # LaTeX
    if args.latex:
        print("\n" + "=" * 90)
        print("  LaTeX SNIPPETS")
        print("=" * 90)
        if not args.conv_only:
            latex_cross_table(df, phys=args.phys)
        if not args.cross_only and has_conv:
            latex_convergence_table(df)


if __name__ == "__main__":
    main()
