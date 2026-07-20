"""Ingest ICON velocity-tendencies wrapper timings into SQLite.

Parses the same log lines as extract_icon_timers.py and writes them to a
database keyed by (cluster, gpu, config, problem, lvn_only, istep, phys,
vt_call), alongside a per-configuration summary.

Usage:
    python utils/ingest_icon_timers.py -o perf_integration.db \\
        --cluster daint --gpu GH200 run/LOG.SAVEME-*.o
"""
import argparse
import sqlite3
import statistics
from pathlib import Path

from extract_icon_timers import extract_vt_timings

SCHEMA = """
CREATE TABLE IF NOT EXISTS timing (
    cluster   TEXT    NOT NULL,
    gpu       TEXT    NOT NULL,
    config    TEXT    NOT NULL,
    problem   TEXT    NOT NULL,
    lvn_only  INTEGER NOT NULL,
    istep     INTEGER NOT NULL,
    phys      INTEGER NOT NULL,
    vt_call   INTEGER NOT NULL,
    time_us   REAL    NOT NULL,
    PRIMARY KEY (cluster, gpu, config, problem, lvn_only, istep, phys, vt_call)
);
CREATE TABLE IF NOT EXISTS timing_summary (
    cluster   TEXT    NOT NULL,
    gpu       TEXT    NOT NULL,
    config    TEXT    NOT NULL,
    problem   TEXT    NOT NULL,
    lvn_only  INTEGER NOT NULL,
    istep     INTEGER NOT NULL,
    n         INTEGER NOT NULL,
    median_us REAL    NOT NULL,
    mean_us   REAL    NOT NULL,
    min_us    REAL    NOT NULL,
    max_us    REAL    NOT NULL,
    PRIMARY KEY (cluster, gpu, config, problem, lvn_only, istep)
);
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("-o", "--output", default="perf_integration.db")
    ap.add_argument("--cluster", required=True, help="e.g. daint")
    ap.add_argument("--gpu", required=True, help="e.g. GH200")
    args = ap.parse_args()

    conn = sqlite3.connect(args.output)
    conn.executescript(SCHEMA)

    rows = []
    for p in args.logs:
        rows.extend(extract_vt_timings(p))
    if not rows:
        print("no timer lines found")
        return 1

    conn.executemany(
        """INSERT OR REPLACE INTO timing
           (cluster, gpu, config, problem, lvn_only, istep, phys, vt_call, time_us)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(args.cluster, args.gpu, r["config"], r["problem"], int(r["lvn_only"]),
          r["istep"], r["phys"], r["vt_call"], r["time_us"]) for r in rows],
    )

    groups: dict[tuple, list[float]] = {}
    for r in rows:
        groups.setdefault(
            (r["config"], r["problem"], int(r["lvn_only"]), r["istep"]), []
        ).append(r["time_us"])

    conn.executemany(
        """INSERT OR REPLACE INTO timing_summary
           (cluster, gpu, config, problem, lvn_only, istep, n, median_us, mean_us, min_us, max_us)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(args.cluster, args.gpu, cfg, prob, lvn, istep, len(v),
          statistics.median(v), statistics.fmean(v), min(v), max(v))
         for (cfg, prob, lvn, istep), v in groups.items()],
    )
    conn.commit()

    print(f"{len(rows)} measurements from {len(args.logs)} logs -> {args.output}")
    print("  %-8s%-8s%-9s%7s%7s%12s" % ("config", "problem", "lvn_only", "istep", "n", "median_us"))
    for (cfg, prob, lvn, istep), v in sorted(groups.items()):
        print("  %-8s%-8s%-9s%7d%7d%12.0f"
              % (cfg, prob, bool(lvn), istep, len(v), statistics.median(v)))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
