import argparse
import sqlite3
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import polars as pl


SKIP_SUBFIELDS = {
    "ddt_w_adv_pc",
    "ddt_vn_apc_pc",
}


def load_data(db_path: str) -> pl.DataFrame:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT phys, field, sub_field, ss_a, ss_b, "
        "SNR_db, vSNR_db "
        "FROM comparisons ORDER BY phys"
    ).fetchall()
    conn.close()
    return pl.DataFrame(rows, schema=[
        "phys", "field", "sub_field", "ss_a", "ss_b",
        "SNR_db", "vSNR_db",
    ], orient="row")


def short_name(field: str, sub_field: str) -> str:
    if sub_field == "-":
        return field
    return sub_field


def enrich(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.when(pl.col("sub_field") == "-")
        .then(pl.col("field"))
        .otherwise(pl.col("sub_field"))
        .alias("name"),
        (pl.col("ss_a").cast(pl.Utf8) + "v" + pl.col("ss_b").cast(pl.Utf8))
        .alias("pair"),
    )


def worst_per_field_at_phys(enriched: pl.DataFrame, phys: int) -> pl.DataFrame:
    """For a single phys step: worst SNR/vSNR per field (across all pairs)."""
    at = enriched.filter(pl.col("phys") == phys)
    if at.is_empty():
        return pl.DataFrame()

    finite = at.filter(pl.col("SNR_db").is_finite())
    if finite.is_empty():
        return pl.DataFrame()

    worst = (
        finite.sort(["name", "SNR_db"])
        .group_by("name", maintain_order=True).first()
        .select([
            "name",
            pl.col("SNR_db").round(1).alias("SNR"),
            pl.col("vSNR_db").round(1).alias("vSNR"),
            pl.col("pair"),
        ])
        .sort("name")
    )
    return worst


def worst_over_timeline(enriched: pl.DataFrame) -> pl.DataFrame:
    """Per field: worst SNR across ALL phys steps, with which step and pair."""
    finite = enriched.filter(pl.col("SNR_db").is_finite())
    if finite.is_empty():
        return pl.DataFrame()

    worst = (
        finite.sort(["name", "SNR_db"])
        .group_by("name", maintain_order=True).first()
        .select([
            "name",
            pl.col("SNR_db").round(1).alias("worst_SNR"),
            pl.col("vSNR_db").round(1).alias("worst_vSNR"),
            pl.col("pair"),
            pl.col("phys").alias("worst_at"),
        ])
        .sort("name")
    )
    return worst


def trend_summary(enriched: pl.DataFrame) -> pl.DataFrame:
    """Per field: SNR at first, last, min, max across timeline (worst pair at each step)."""
    finite = enriched.filter(pl.col("SNR_db").is_finite())
    if finite.is_empty():
        return pl.DataFrame()

    # Worst SNR per (phys, name)
    per_step = (
        finite.sort(["phys", "name", "SNR_db"])
        .group_by(["phys", "name"], maintain_order=True).first()
        .select(["phys", "name", "SNR_db", "vSNR_db"])
    )

    phys_min = per_step["phys"].min()
    phys_max = per_step["phys"].max()

    first = (
        per_step.filter(pl.col("phys") == phys_min)
        .select([
            "name",
            pl.col("SNR_db").round(1).alias("first_SNR"),
        ])
    )
    last = (
        per_step.filter(pl.col("phys") == phys_max)
        .select([
            "name",
            pl.col("SNR_db").round(1).alias("last_SNR"),
        ])
    )
    envelope = (
        per_step.group_by("name").agg([
            pl.col("SNR_db").min().round(1).alias("min_SNR"),
            pl.col("SNR_db").max().round(1).alias("max_SNR"),
            pl.col("SNR_db").mean().round(1).alias("mean_SNR"),
        ])
    )

    return (
        first
        .join(last, on="name")
        .join(envelope, on="name")
        .with_columns(
            (pl.col("first_SNR") - pl.col("last_SNR")).round(1).alias("drift")
        )
        .select(["name", "first_SNR", "last_SNR", "drift", "min_SNR", "max_SNR", "mean_SNR"])
        .sort("name")
    )


def fmt_inf(v) -> str:
    if isinstance(v, float):
        if v == float("inf"):
            return "-"
        elif v == float("-inf"):
            return "-inf"
        else:
            return f"{v:.1f}"
    return str(v)


def df_to_md(df: pl.DataFrame, title: str) -> str:
    lines = [f"### {title}", ""]
    cols = df.columns
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("| " + " | ".join("---" for _ in cols) + " |")
    for row in df.iter_rows():
        lines.append("| " + " | ".join(fmt_inf(v) for v in row) + " |")
    lines.append("")
    return "\n".join(lines)


def print_section(title: str, df: pl.DataFrame):
    print(f"\n{'=' * 70}")
    print(title)
    print("=" * 70)
    with pl.Config(tbl_rows=100, tbl_width_chars=200, tbl_cols=-1, float_precision=1):
        print(df)


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

# Publication-quality defaults
PLOT_STYLE = {
    "font.family": "serif",
    "font.size": 10,
    "axes.labelsize": 11,
    "axes.titlesize": 12,
    "legend.fontsize": 9,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.05,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "lines.linewidth": 1.5,
    "lines.markersize": 4,
}

# Color by grid (Tol vibrant, colorblind-safe)
GRID_COLORS = ["#0077BB", "#EE7733", "#009988", "#CC3311", "#AA3377", "#33BBEE"]

# Marker by variable
FIELD_MARKERS = {
    "vt": "D",
    "w": "s",
    "vn": "o",
    "vn_ie": "^",
    "w_concorr_c": "v",
    "p_metrics": "P",
}
_FALLBACK_MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*"]


PLOT_FIELDS = {"vt", "w"}
PLOT_LABELS = {"vt": "vt (best)", "w": "w (worst)"}


def _reorder_legend_row_major(handles, ncol):
    """Reorder handles so legend fills row-major instead of matplotlib's column-major."""
    n = len(handles)
    nrow = (n + ncol - 1) // ncol
    # Pad with None to fill grid
    padded = handles + [None] * (nrow * ncol - n)
    # Column-major indices that matplotlib expects, given our row-major input
    reordered = []
    for col in range(ncol):
        for row in range(nrow):
            idx = row * ncol + col
            if idx < len(padded) and padded[idx] is not None:
                reordered.append(padded[idx])
    return reordered

# All grids use solid lines — distinguished by color instead
GRID_LINESTYLES = ["-", "-", "-"]


def _extract_grid_label(db_label: str) -> str:
    """Extract short grid label like 'B05' from db name."""
    import re
    m = re.search(r"R\d+(B\d+)", db_label)
    return m.group(1) if m else db_label


def plot_incremental_snr(
    datasets: list[tuple[pl.DataFrame, str]],  # [(enriched, db_label), ...]
    out_dir: Path,
) -> Optional[Path]:
    """Plot 1: For each (grid, field), SNR of consecutive and direct pairs."""
    plt.rcParams.update(PLOT_STYLE)
    from matplotlib.lines import Line2D

    all_ss = [5, 6, 7, 8, 9, 10]
    consecutive_pairs = [(s, s + 1) for s in all_ss[:-1]]
    direct_pairs = [(s, 10) for s in all_ss[:-1]]

    fig, ax = plt.subplots(figsize=(5.5, 4))

    # Collect per-grid data for reordered plotting
    grid_data = []  # [(g_idx, grid, ls, finite, names), ...]
    for g_idx, (enriched, db_label) in enumerate(datasets):
        phys_vals = sorted(enriched["phys"].unique().to_list())
        mid_phys = phys_vals[1] if len(phys_vals) > 1 else phys_vals[0]
        grid = _extract_grid_label(db_label)
        ls = GRID_LINESTYLES[g_idx % len(GRID_LINESTYLES)]

        finite = enriched.filter(
            (pl.col("phys") == mid_phys) & pl.col("SNR_db").is_finite()
        )
        if finite.is_empty():
            continue

        names = [n for n in sorted(finite["name"].unique().to_list()) if n in PLOT_FIELDS]
        grid_data.append((g_idx, grid, ls, finite, names))

    # All field names across grids
    all_names = sorted(set(n for _, _, _, _, ns in grid_data for n in ns))

    # Plot field-first, grid-second so legend groups by field
    for i, name in enumerate(all_names):
        m = FIELD_MARKERS.get(name, _FALLBACK_MARKERS[i % len(_FALLBACK_MARKERS)])
        for g_idx, grid, ls, finite, names in grid_data:
            if name not in names:
                continue
            c = GRID_COLORS[g_idx % len(GRID_COLORS)]
            lbl = f"{grid} {PLOT_LABELS.get(name, name)}"

            def _get_snr(name, ss_a, ss_b, _fin=finite):
                row = _fin.filter(
                    (pl.col("name") == name) &
                    (pl.col("ss_a") == ss_a) &
                    (pl.col("ss_b") == ss_b)
                )
                return row["SNR_db"][0] if not row.is_empty() else float("nan")

            dir_snrs = [_get_snr(name, a, b) for a, b in direct_pairs]
            ax.plot(all_ss[:-1], dir_snrs, color=c, marker=m, label=lbl, linestyle=ls)

            inc_snrs = [_get_snr(name, a, b) for a, b in consecutive_pairs]
            ax.plot(all_ss[:-1], inc_snrs, color=c, marker=m, linestyle=":", alpha=0.4)

    # Finalize ylim, then draw ghost lines to a fixed point above
    ax.set_ylim(bottom=0)
    ylo, yhi = ax.get_ylim()
    cap = yhi + (yhi - ylo) * 0.05
    above = cap + (yhi - ylo) * 0.2  # fixed target above cap, same for all
    ax.set_ylim(top=cap)

    for g_idx, (enriched, db_label) in enumerate(datasets):
        phys_vals = sorted(enriched["phys"].unique().to_list())
        mid_phys = phys_vals[1] if len(phys_vals) > 1 else phys_vals[0]
        ls = GRID_LINESTYLES[g_idx % len(GRID_LINESTYLES)]
        finite_g = enriched.filter(
            (pl.col("phys") == mid_phys) & pl.col("SNR_db").is_finite()
        )
        names_g = [n for n in sorted(finite_g["name"].unique().to_list()) if n in PLOT_FIELDS]
        def _get_snr_g(name, ss_a, ss_b):
            row = finite_g.filter(
                (pl.col("name") == name) & (pl.col("ss_a") == ss_a) & (pl.col("ss_b") == ss_b)
            )
            return row["SNR_db"][0] if not row.is_empty() else float("nan")
        for i, name in enumerate(names_g):
            c = GRID_COLORS[g_idx % len(GRID_COLORS)]
            snr_at_9 = _get_snr_g(name, 9, 10)
            if np.isfinite(snr_at_9):
                ax.plot([9, 10], [snr_at_9, above], color=c, linestyle=ls, clip_on=True)
                ax.plot([9, 10], [snr_at_9, above], color=c, linestyle=":", alpha=0.5, clip_on=True)

    ax.set_xlabel("#substeps")
    ax.set_ylabel("SNR (dB)")
    ax.set_title("SNR loss from coarsening")
    ax.set_xticks(all_ss)
    ax.invert_xaxis()
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=False, nbins=8))

    handles, _ = ax.get_legend_handles_labels()
    handles.append(Line2D([0], [0], color="gray", linestyle="-", label="vs substep=10"))
    handles.append(Line2D([0], [0], color="gray", linestyle=":", alpha=0.4, label="incremental"))
    ax.legend(
        handles=_reorder_legend_row_major(handles, 3),
        loc="upper center", bbox_to_anchor=(0.5, -0.15),
        ncol=3, framealpha=0.9, columnspacing=0.8, handletextpad=0.4,
    )
    fig.subplots_adjust(bottom=0.35)

    path = out_dir / "incremental_snr.pdf"
    fig.savefig(path)
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def plot_snr_timeline(
    datasets: list[tuple[pl.DataFrame, str]],
    out_dir: Path,
) -> Optional[Path]:
    """Plot 2: For each (grid, field), worst-pair SNR over physics steps."""
    plt.rcParams.update(PLOT_STYLE)

    fig, ax = plt.subplots(figsize=(5.5, 4))

    # Collect per-grid data
    grid_data = []
    for g_idx, (enriched, db_label) in enumerate(datasets):
        grid = _extract_grid_label(db_label)
        ls = GRID_LINESTYLES[g_idx % len(GRID_LINESTYLES)]

        finite = enriched.filter(pl.col("SNR_db").is_finite())
        if finite.is_empty():
            continue

        per_step = (
            finite.sort(["phys", "name", "SNR_db"])
            .group_by(["phys", "name"], maintain_order=True).first()
            .select(["phys", "name", "SNR_db"])
        )

        names = [n for n in sorted(per_step["name"].unique().to_list()) if n in PLOT_FIELDS]
        grid_data.append((g_idx, grid, ls, per_step, names))

    all_names = sorted(set(n for _, _, _, _, ns in grid_data for n in ns))

    # Plot field-first, grid-second
    for i, name in enumerate(all_names):
        m = FIELD_MARKERS.get(name, _FALLBACK_MARKERS[i % len(_FALLBACK_MARKERS)])
        for g_idx, grid, ls, per_step, names in grid_data:
            if name not in names:
                continue
            subset = per_step.filter(pl.col("name") == name).sort("phys")
            ax.plot(
                subset["phys"].to_list(),
                subset["SNR_db"].to_list(),
                color=GRID_COLORS[g_idx % len(GRID_COLORS)],
                marker=m,
                label=f"{grid} {PLOT_LABELS.get(name, name)}",
                linestyle=ls,
                markevery=max(1, len(subset) // 10),
            )

    ax.set_ylim(bottom=0)
    ax.set_xlabel("Physics step (p)")
    ax.set_ylabel("SNR (dB)")
    ax.set_title("SNR drift over time")
    ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=False, nbins=8))
    handles, _ = ax.get_legend_handles_labels()
    ax.legend(
        handles=_reorder_legend_row_major(handles, 3),
        loc="upper center", bbox_to_anchor=(0.5, -0.22),
        ncol=3, framealpha=0.9, columnspacing=0.8, handletextpad=0.4,
    )
    fig.subplots_adjust(bottom=0.33)

    path = out_dir / "snr_timeline.pdf"
    fig.savefig(path)
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description="Report convergence from v3 SQLite DB.")
    parser.add_argument("db", nargs="+", help="Path(s) to convergence.db")
    parser.add_argument("-o", "--output", default=None,
                        help="Output markdown report (default: <db>.report.md)")
    parser.add_argument("--plots", action="store_true",
                        help="Generate PDF plots (saved next to DB)")
    args = parser.parse_args()

    md_parts = ["# Convergence Report\n"]
    all_datasets = []  # [(enriched, db_label), ...] for combined plots

    for db_path in args.db:
        df = load_data(db_path)
        if df.is_empty():
            print(f"No data in {db_path}.")
            continue

        df = df.filter(~pl.col("sub_field").is_in(SKIP_SUBFIELDS))
        # Drop rows where sub_field is "-" and field has other sub_fields
        fields_with_subs = df.filter(pl.col("sub_field") != "-").select("field").unique()["field"].to_list()
        df = df.filter(~((pl.col("sub_field") == "-") & pl.col("field").is_in(fields_with_subs)))

        enriched = enrich(df)

        names = enriched.select("name").unique().sort("name")
        phys_min = int(enriched["phys"].min())
        phys_max = int(enriched["phys"].max())
        first_step = phys_min + 1  # first diverging step

        db_label = Path(db_path).stem
        all_datasets.append((enriched, db_label))

        print(f"\n{'#' * 70}")
        print(f"  {db_label}")
        print(f"  Fields: {', '.join(names['name'].to_list())}")
        print(f"  Phys range: {phys_min}..{phys_max}")
        print(f"{'#' * 70}")

        md_parts.append(f"## {db_label}\n")
        md_parts.append(f"Phys range: {phys_min}..{phys_max}\n")

        # 1) First diverging step
        t1 = worst_per_field_at_phys(enriched, first_step)
        if not t1.is_empty():
            print_section(f"First step (p={first_step})", t1)
            md_parts.append(df_to_md(t1, f"First step (p={first_step})"))

        # 2) Last step
        t2 = worst_per_field_at_phys(enriched, phys_max)
        if not t2.is_empty():
            print_section(f"Last step (p={phys_max})", t2)
            md_parts.append(df_to_md(t2, f"Last step (p={phys_max})"))

        # 3) Worst over entire timeline
        t3 = worst_over_timeline(enriched)
        if not t3.is_empty():
            print_section("Worst over timeline", t3)
            md_parts.append(df_to_md(t3, "Worst over timeline"))

        # 4) Trend summary: drift from first to last
        t4 = trend_summary(enriched)
        if not t4.is_empty():
            print_section("Trend (worst-pair SNR per step)", t4)
            md_parts.append(df_to_md(t4, "Trend (worst-pair SNR per step)"))

    # Combined plots across all DBs
    if args.plots and all_datasets:
        plot_dir = Path(args.db[0]).parent
        p1 = plot_incremental_snr(all_datasets, plot_dir)
        p2 = plot_snr_timeline(all_datasets, plot_dir)
        if p1:
            md_parts.append(f"![Incremental SNR]({p1.name})\n")
        if p2:
            md_parts.append(f"![SNR Timeline]({p2.name})\n")

    if args.output:
        md = "\n".join(md_parts)
        Path(args.output).write_text(md)
        print(f"\nSaved to: {args.output}")


if __name__ == "__main__":
    main()
