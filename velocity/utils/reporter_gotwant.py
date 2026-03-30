"""Reporter for gotwant.db files (compare_got_and_want output).

Reads one or more gotwant_*.db files and optional companion .log files
(from the standalone GPU binary) to produce a combined accuracy + perf report.

Usage:
    python -m utils.reporter_gotwant gotwant_f64.db gotwant_f32.db gotwant_f16.db
    python -m utils.reporter_gotwant gotwant_f64.db --log f64.log
    python -m utils.reporter_gotwant gotwant_f64.db gotwant_f32.db --logs f64.log f32.log
"""

import argparse
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import polars as pl


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

_STEP_RE = re.compile(
    r"Step (?P<ts>\d+) variables.*?istep:\s*(?P<istep>\d+).*?lvn_only:\s*(?P<lvn>\d+)"
)
_TIMER_RE = re.compile(
    r"Timer (?P<kernel>\S+) took (?P<us>\d+) us"
)
_GPU_XFER_RE = re.compile(
    r"GPU Bytes Transferred:\s*(?P<bytes>\d+)\s*\((?P<mb>[\d.]+) MB\)"
)
_GPU_BASELINE_RE = re.compile(
    r"Baseline Real GPU Memory:\s*(?P<mb>[\d.]+) MB"
)
_GPU_INTERNAL_RE = re.compile(
    r"Internal GPU Bytes Allocated:\s*(?P<bytes>\d+)\s*\((?P<mb>[\d.]+) MB\)"
)
_GPU_AFTER_RE = re.compile(
    r"Real GPU Memory after init:\s*(?P<mb>[\d.]+) MB"
)
_GPU_RESIDENCY_RE = re.compile(
    r"Residency Increase:\s*(?P<mb>[\d.]+) MB"
)


def parse_log(path: Path) -> List[Dict[str, Any]]:
    """Parse a standalone binary log into per-timestep records."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    records: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    for line in lines:
        m = _STEP_RE.search(line)
        if m:
            current = {
                "timestep": int(m.group("ts")),
                "istep": int(m.group("istep")),
                "lvn_only": int(m.group("lvn")),
            }
            continue

        m = _TIMER_RE.search(line)
        if m:
            current["kernel"] = m.group("kernel")
            current["time_us"] = int(m.group("us"))
            records.append(current)
            current = {}
            continue

        m = _GPU_XFER_RE.search(line)
        if m:
            current["h2d_mb"] = float(m.group("mb"))
            continue

        m = _GPU_BASELINE_RE.search(line)
        if m:
            current["baseline_mb"] = float(m.group("mb"))
            continue

        m = _GPU_INTERNAL_RE.search(line)
        if m:
            current["internal_mb"] = float(m.group("mb"))
            continue

        m = _GPU_AFTER_RE.search(line)
        if m:
            current["after_init_mb"] = float(m.group("mb"))
            continue

        m = _GPU_RESIDENCY_RE.search(line)
        if m:
            current["residency_mb"] = float(m.group("mb"))
            continue

    return records


def log_to_dataframe(records: List[Dict[str, Any]]) -> pl.DataFrame:
    if not records:
        return pl.DataFrame()
    return pl.DataFrame(records)


# ---------------------------------------------------------------------------
# DB loading
# ---------------------------------------------------------------------------

SKIP_SUBFIELDS = {
    "ddt_w_adv_pc",
    "ddt_vn_apc_pc",
}

# Fields that are read-only inputs — OK rows are expected and uninteresting
INPUT_FIELDS = {
    "p_int", "p_patch", "global_data",
}


def load_gotwant_db(db_path: str) -> pl.DataFrame:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT timestep, field, sub_field, status, "
        "max_abs, max_rel, mae, rmse, SNR_db, vSNR_db, "
        "mismatches, total_elements "
        "FROM comparisons ORDER BY timestep, field, sub_field"
    ).fetchall()
    conn.close()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema=[
        "timestep", "field", "sub_field", "status",
        "max_abs", "max_rel", "mae", "rmse", "SNR_db", "vSNR_db",
        "mismatches", "total_elements",
    ], orient="row")


def enrich(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.when(pl.col("sub_field") == "-")
        .then(pl.col("field"))
        .otherwise(pl.col("field") + " % " + pl.col("sub_field"))
        .alias("name"),
    )


# ---------------------------------------------------------------------------
# Accuracy report
# ---------------------------------------------------------------------------


def accuracy_summary(df: pl.DataFrame) -> pl.DataFrame:
    """Per-field worst-case accuracy across all timesteps."""
    finite = df.filter(
        pl.col("SNR_db").is_finite() & pl.col("SNR_db").is_not_nan()
    )
    if finite.is_empty():
        # All exact matches
        return (
            df.group_by("name", maintain_order=True)
            .agg([
                pl.lit("OK").alias("status"),
                pl.col("total_elements").first(),
                pl.lit(float("inf")).alias("worst_SNR"),
                pl.lit(float("inf")).alias("worst_vSNR"),
                pl.lit(0.0).alias("worst_max_abs"),
                pl.lit(0.0).alias("worst_rmse"),
                pl.lit(0).cast(pl.Int64).alias("worst_mismatches"),
            ])
            .sort("name")
        )

    worst = (
        finite.sort(["name", "SNR_db"])
        .group_by("name", maintain_order=True).first()
        .select([
            "name",
            pl.col("status"),
            pl.col("total_elements"),
            pl.col("SNR_db").round(1).alias("worst_SNR"),
            pl.col("vSNR_db").round(1).alias("worst_vSNR"),
            pl.col("max_abs").alias("worst_max_abs"),
            pl.col("rmse").alias("worst_rmse"),
            pl.col("mismatches").alias("worst_mismatches"),
            pl.col("timestep").alias("worst_at"),
        ])
        .sort("name")
    )

    # Add exact-match fields that were filtered out
    exact_names = set(df["name"].unique().to_list()) - set(finite["name"].unique().to_list())
    if exact_names:
        exact = (
            df.filter(pl.col("name").is_in(exact_names))
            .group_by("name", maintain_order=True).first()
            .select([
                "name",
                pl.lit("OK").alias("status"),
                pl.col("total_elements"),
                pl.lit(float("inf")).alias("worst_SNR"),
                pl.lit(float("inf")).alias("worst_vSNR"),
                pl.lit(0.0).alias("worst_max_abs"),
                pl.lit(0.0).alias("worst_rmse"),
                pl.lit(0).cast(pl.Int64).alias("worst_mismatches"),
                pl.lit(-1).cast(pl.Int64).alias("worst_at"),
            ])
        )
        worst = pl.concat([worst, exact]).sort("name")

    return worst


def per_timestep_summary(df: pl.DataFrame) -> pl.DataFrame:
    """Per-timestep: worst SNR across all fields."""
    finite = df.filter(
        pl.col("SNR_db").is_finite() & pl.col("SNR_db").is_not_nan()
    )
    if finite.is_empty():
        return pl.DataFrame()

    return (
        finite.sort(["timestep", "SNR_db"])
        .group_by("timestep", maintain_order=True).first()
        .select([
            "timestep",
            "name",
            pl.col("SNR_db").round(1).alias("worst_SNR"),
            pl.col("vSNR_db").round(1).alias("worst_vSNR"),
            pl.col("max_abs"),
            pl.col("mismatches"),
            pl.col("total_elements"),
        ])
        .sort("timestep")
    )


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def fmt_inf(v) -> str:
    if isinstance(v, float):
        if v == float("inf"):
            return "exact"
        elif v == float("-inf"):
            return "-inf"
        elif abs(v) < 1e-3 or abs(v) > 1e6:
            return f"{v:.2e}"
        else:
            return f"{v:.2f}"
    return str(v)


def print_section(title: str, df: pl.DataFrame):
    print(f"\n{'=' * 80}")
    print(title)
    print("=" * 80)
    with pl.Config(tbl_rows=200, tbl_width_chars=220, tbl_cols=-1, float_precision=2):
        print(df)


def print_perf_section(title: str, log_df: pl.DataFrame):
    # Pick columns that actually exist in the parsed log
    agg_cols = [pl.col("kernel").first(), pl.col("time_us").first()]
    for col in ("h2d_mb", "internal_mb", "residency_mb", "baseline_mb"):
        if col in log_df.columns:
            agg_cols.append(pl.col(col).first())

    summary = (
        log_df.group_by("timestep", maintain_order=True)
        .agg(agg_cols)
        .sort("timestep")
    )
    print_section(title, summary)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _label_from_path(db_path: str) -> str:
    """Extract short label like 'f64' from 'gotwant_f64.db'."""
    m = re.match(r"gotwant_(.+)\.db$", Path(db_path).name)
    return m.group(1).upper() if m else Path(db_path).stem


def _load_and_filter(db_path: str, skip_inputs: bool) -> pl.DataFrame:
    df = load_gotwant_db(db_path)
    if df.is_empty():
        return df

    df = df.filter(~pl.col("sub_field").is_in(SKIP_SUBFIELDS))

    # Drop rows where sub_field is "-" and field has other sub_fields
    fields_with_subs = (
        df.filter(pl.col("sub_field") != "-")
        .select("field").unique()["field"].to_list()
    )
    df = df.filter(
        ~((pl.col("sub_field") == "-") & pl.col("field").is_in(fields_with_subs))
    )

    if skip_inputs:
        df = df.filter(~pl.col("field").is_in(INPUT_FIELDS))

    return enrich(df)


def worst_snr_per_field(df: pl.DataFrame) -> pl.DataFrame:
    """Per field: worst SNR and vSNR across all timesteps."""
    finite = df.filter(
        pl.col("SNR_db").is_finite() & pl.col("SNR_db").is_not_nan()
    )

    # Fields with finite SNR — take worst
    if not finite.is_empty():
        worst = (
            finite.sort(["name", "SNR_db"])
            .group_by("name", maintain_order=True).first()
            .select([
                "name",
                pl.col("SNR_db").round(1),
                pl.col("vSNR_db").round(1),
            ])
        )
    else:
        worst = pl.DataFrame(schema={"name": pl.Utf8, "SNR_db": pl.Float64, "vSNR_db": pl.Float64})

    # Fields that are exact (inf SNR)
    exact_names = set(df["name"].unique().to_list()) - set(finite["name"].unique().to_list()) if not finite.is_empty() else set(df["name"].unique().to_list())
    if exact_names:
        exact = pl.DataFrame({
            "name": sorted(exact_names),
            "SNR_db": [float("inf")] * len(exact_names),
            "vSNR_db": [float("inf")] * len(exact_names),
        })
        worst = pl.concat([worst, exact])

    return worst.sort("name")


def combined_snr_table(
    db_paths: List[str],
    skip_inputs: bool,
    timesteps: Optional[List[int]] = None,
) -> pl.DataFrame:
    """Build a single table with SNR/vSNR columns for each precision side by side.

    If *timesteps* is given, only consider those timesteps.
    """
    pieces: Dict[str, pl.DataFrame] = {}

    for db_path in db_paths:
        label = _label_from_path(db_path)
        df = _load_and_filter(db_path, skip_inputs)
        if df.is_empty():
            continue
        if timesteps is not None:
            df = df.filter(pl.col("timestep").is_in(timesteps))
            if df.is_empty():
                continue
        worst = worst_snr_per_field(df)
        pieces[label] = worst.rename({
            "SNR_db": f"SNR_{label}",
            "vSNR_db": f"vSNR_{label}",
        })

    if not pieces:
        return pl.DataFrame()

    labels = list(pieces.keys())
    result = pieces[labels[0]]
    for label in labels[1:]:
        result = result.join(pieces[label], on="name", how="full", coalesce=True)

    return result.sort("name")


def _variant_timesteps(db_paths: List[str], log_paths: List[str]) -> Dict[str, List[int]]:
    """Map variant label → list of timesteps, using the first available log."""
    for i, db_path in enumerate(db_paths):
        log_path = _resolve_log(db_path, log_paths, i)
        if log_path is None:
            continue
        records = parse_log(log_path)
        if not records:
            continue
        groups: Dict[str, List[int]] = {}
        for r in records:
            lvn = r.get("lvn_only", "?")
            istep = r.get("istep", "?")
            key = f"lvn={lvn} istep={istep}"
            groups.setdefault(key, []).append(r["timestep"])
        if groups:
            return groups
    return {}


def _resolve_log(db_path: str, log_paths: List[str], idx: int) -> Optional[Path]:
    if idx < len(log_paths) and log_paths[idx] != "-":
        p = Path(log_paths[idx])
        return p if p.is_file() else None
    m = re.match(r"gotwant_(.+)\.db$", Path(db_path).name)
    if m:
        candidate = Path(db_path).parent / f"{m.group(1)}.log"
        if candidate.is_file():
            return candidate
    return None



def _log_per_timestep(log_path: Path) -> pl.DataFrame:
    """Parse log → per-timestep summary with variant, time_us, internal_mb, residency_mb."""
    records = parse_log(log_path)
    if not records:
        return pl.DataFrame()
    log_df = log_to_dataframe(records)

    # Add variant column from lvn_only + istep
    if "lvn_only" in log_df.columns and "istep" in log_df.columns:
        log_df = log_df.with_columns(
            (pl.lit("lvn=") + pl.col("lvn_only").cast(pl.Utf8)
             + pl.lit(" istep=") + pl.col("istep").cast(pl.Utf8))
            .alias("variant")
        )
    else:
        log_df = log_df.with_columns(pl.lit("-").alias("variant"))

    cols = ["variant", "time_us"]
    for c in ("internal_mb",):
        if c in log_df.columns:
            cols.append(c)
    return (
        log_df.group_by("timestep", maintain_order=True)
        .agg([pl.col(c).first() for c in cols])
        .sort("timestep")
    )


def combined_perf_tables(db_paths: List[str], log_paths: List[str]):
    """Build separate time (µs) and memory (MB) tables across precisions."""
    time_pieces: Dict[str, pl.DataFrame] = {}
    mem_pieces: Dict[str, pl.DataFrame] = {}
    variant_col: Optional[pl.DataFrame] = None

    for i, db_path in enumerate(db_paths):
        label = _label_from_path(db_path)
        log_path = _resolve_log(db_path, log_paths, i)
        if log_path is None:
            continue
        per_ts = _log_per_timestep(log_path)
        if per_ts.is_empty():
            continue

        # Keep variant from first log only (same across precisions)
        if variant_col is None and "variant" in per_ts.columns:
            variant_col = per_ts.select(["timestep", "variant"])

        # Time table
        if "time_us" in per_ts.columns:
            time_pieces[label] = per_ts.select(
                ["timestep", "time_us"]
            ).rename({"time_us": f"us_{label}"})

        # Memory table
        if "internal_mb" in per_ts.columns:
            mem_pieces[label] = per_ts.select(
                ["timestep", "internal_mb"]
            ).rename({"internal_mb": f"MB_{label}"})

    for title, pieces in [
        ("Kernel time (µs)", time_pieces),
        ("Internal GPU allocation (MB)", mem_pieces),
    ]:
        if not pieces:
            continue
        labels = list(pieces.keys())
        result = pieces[labels[0]]
        for label in labels[1:]:
            result = result.join(pieces[label], on="timestep", how="full", coalesce=True)
        if variant_col is not None:
            result = variant_col.join(result, on="timestep", how="right", coalesce=True)
        result = result.sort("timestep")
        print_section(title, result)


def main():
    parser = argparse.ArgumentParser(
        description="Report got-vs-want accuracy (and optional perf from logs)."
    )
    parser.add_argument("db", nargs="+", help="Path(s) to gotwant_*.db")
    parser.add_argument("--logs", nargs="*", default=None,
                        help="Companion .log files (matched by order to db args). "
                             "Use '-' to skip a db.")
    parser.add_argument("--skip-inputs", action="store_true", default=True,
                        help="Hide read-only input fields (default: true)")
    parser.add_argument("--no-skip-inputs", action="store_false", dest="skip_inputs")
    args = parser.parse_args()

    # -- Print inputs --
    log_paths = args.logs or []
    print("Databases:")
    for db_path in args.db:
        print(f"  {db_path}")
    resolved_logs = [_resolve_log(db, log_paths, i) for i, db in enumerate(args.db)]
    if any(resolved_logs):
        print("Logs:")
        for lp in resolved_logs:
            print(f"  {lp or '(none)'}")
    print()

    # -- Per-variant SNR/vSNR tables --
    variant_groups = _variant_timesteps(args.db, log_paths)
    for variant, ts_list in sorted(variant_groups.items()):
        tbl = combined_snr_table(args.db, args.skip_inputs, timesteps=ts_list)
        if not tbl.is_empty():
            print_section(f"SNR / vSNR — {variant} (dB)", tbl)

    # -- Overall SNR/vSNR table --
    combined = combined_snr_table(args.db, args.skip_inputs)
    if not combined.is_empty():
        print_section("SNR / vSNR comparison (worst across all timesteps, dB)", combined)

    # -- Combined perf table from logs --
    combined_perf_tables(args.db, args.logs or [])


if __name__ == "__main__":
    main()
