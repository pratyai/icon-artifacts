"""Extract velocity-tendencies per-call timings from ICON log files.

Handles two timer formats:
  - OG (Fortran wrapper):  "host side timing:  1267.0  us, lvn_only:  T , istep:  1"
  - DaCe wrapper:          "Timer velocity_no_nproma_if_prop_lvn_only_0_istep_1 took 917 us"

Physics timestep from: "Time step:  74, model time: ..."

Usage:
    python extract_icon_timers.py ICON-LOGS-LOWER/LOG.SAVEME-*.o
    python extract_icon_timers.py -o vt_timers.csv logs/*.o
"""
import argparse
import re
from pathlib import Path

import polars as pl


OG_RE = re.compile(
    r"host side timing:\s+([\d.]+)\s+us,\s+lvn_only:\s+([FT])\s*,\s+istep:\s+(\d+)"
)
DACE_RE = re.compile(
    r"Timer\s+velocity_no_nproma_if_prop_lvn_only_(\d+)_istep_(\d+)\s+took\s+(\d+)\s+us"
)
TIMESTEP_RE = re.compile(r"Time step:\s+(\d+),")


def extract_config_label(path: Path) -> str:
    """Extract config from filename.

    LOG.SAVEME-F16.dt8_v5_g0008_R02B05.o            -> F16
    LOG.SAVEME-SER-BF16.sc2026_dt8_ss5_....4242439.o -> BF16

    Serialization-enabled runs carry a `SER-` segment before the precision, so
    the label is the last hyphen-separated token before the first dot.
    """
    m = re.search(r"SAVEME-([\w-]+?)\.", path.name)
    return m.group(1).rsplit("-", 1)[-1] if m else path.stem


def extract_problem(path: Path) -> str:
    """Extract shortened problem from filename, e.g. ...g0008_R02B05.o -> B05."""
    m = re.search(r"R\d+(B\d+)", path.name)
    return m.group(1) if m else "unknown"


def extract_vt_timings(path: Path) -> list[dict]:
    label = extract_config_label(path)
    problem = extract_problem(path)
    rows = []
    phys_step = 0
    vt_idx = 0  # resets each physics timestep

    for line in path.open(errors="replace"):
        m = TIMESTEP_RE.search(line)
        if m:
            phys_step = int(m.group(1))
            vt_idx = 0
            continue

        m = OG_RE.search(line)
        if m:
            vt_idx += 1
            rows.append({
                "problem": problem,
                "phys": phys_step,
                "vt_call": vt_idx,
                "config": label,
                "lvn_only": m.group(2) == "T",
                "istep": int(m.group(3)),
                "time_us": float(m.group(1)),
            })
            continue

        m = DACE_RE.search(line)
        if m:
            vt_idx += 1
            rows.append({
                "problem": problem,
                "phys": phys_step,
                "vt_call": vt_idx,
                "config": label,
                "lvn_only": m.group(1) == "1",
                "istep": int(m.group(2)),
                "time_us": float(m.group(3)),
            })

    return rows


def main():
    parser = argparse.ArgumentParser(description="Extract VT per-call timings from ICON logs.")
    parser.add_argument("logs", nargs="+", help="Log files to process")
    parser.add_argument("-o", "--output", default=None, help="Output CSV path")
    args = parser.parse_args()

    all_rows = []
    for logfile in sorted(args.logs):
        rows = extract_vt_timings(Path(logfile))
        all_rows.extend(rows)

    if not all_rows:
        print("No VT timings found.")
        return

    df = pl.DataFrame(all_rows, orient="row").select([
        "problem", "phys", "vt_call", "config", "lvn_only", "istep", "time_us",
    ])

    if args.output:
        df.write_csv(args.output)
        print(f"{len(df)} rows saved to: {args.output}")

    # Aggregation: skip phys<=1 warmup, then drop the single largest outlier per group
    filtered = (
        df.filter(pl.col("phys") > 1)
        .with_columns(
            pl.col("time_us").rank("ordinal", descending=True)
            .over(["problem", "config", "lvn_only", "istep"])
            .alias("_rank")
        )
        .filter(pl.col("_rank") > 1)  # drop the single largest value per group
        .drop("_rank")
    )

    agg = (
        filtered
        .group_by(["problem", "config", "lvn_only", "istep"])
        .agg([
            pl.col("time_us").count().alias("N"),
            pl.col("time_us").mean().alias("mean"),
            pl.col("time_us").median().alias("median"),
            pl.col("time_us").std().alias("std"),
            pl.col("time_us").min().alias("min"),
            pl.col("time_us").max().alias("max"),
            pl.col("time_us").quantile(0.05).alias("p05"),
            pl.col("time_us").quantile(0.95).alias("p95"),
        ])
        .sort(["problem", "config", "lvn_only", "istep"])
    )

    print(f"\n{len(df)} total measurements, {len(filtered)} after outlier removal")
    print("\n=== VT Timing Summary (us, 1 outlier removed per group) ===")
    with pl.Config(tbl_rows=50, tbl_width_chars=250, tbl_cols=-1, float_precision=1):
        print(agg)


if __name__ == "__main__":
    main()
