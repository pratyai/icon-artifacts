import argparse
from concurrent.futures import ProcessPoolExecutor
from itertools import zip_longest
import os
import re
import sqlite3
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple
from functools import partial
import numpy as np
from pathlib import Path
import zstandard as zstd

sys.stdout.reconfigure(line_buffering=True)

DEFAULT_WORKERS = int(
    os.environ.get(
        "SLURM_CPUS_PER_TASK", os.environ.get("SLURM_CPUS_ON_NODE", os.cpu_count() or 1)
    )
)

# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS comparisons (
    timestep       INTEGER NOT NULL,
    field          TEXT    NOT NULL,
    sub_field      TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    max_abs        REAL,
    max_rel        REAL,
    mae            REAL,
    rmse           REAL,
    SNR_db         REAL,
    vSNR_db        REAL,
    mismatches     INTEGER,
    total_elements INTEGER,
    PRIMARY KEY (timestep, field, sub_field)
);
CREATE INDEX IF NOT EXISTS idx_timestep ON comparisons (timestep);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def timestep_exists(conn: sqlite3.Connection, timestep: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM comparisons WHERE timestep=? LIMIT 1", (timestep,)
    ).fetchone()
    return row is not None


def insert_rows(conn: sqlite3.Connection, rows: List[Dict[str, Any]]):
    if not rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO comparisons
           (timestep, field, sub_field, status,
            max_abs, max_rel, mae, rmse, SNR_db, vSNR_db,
            mismatches, total_elements)
           VALUES (:timestep, :field, :sub_field, :status,
                   :max_abs, :max_rel, :mae, :rmse, :SNR_db, :vSNR_db,
                   :mismatches, :total_elements)""",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def discover_timesteps(root: Path) -> List[int]:
    ts_set: set[int] = set()
    pat = re.compile(r"_(\d+)\.got(?:\.zst)?$")
    for p in [*root.glob("*.got"), *root.glob("*.got.zst")]:
        m = pat.search(p.name)
        if m:
            ts_set.add(int(m.group(1)))
    pat_w = re.compile(r"_(\d+)\.want(?:\.zst)?$")
    for p in [*root.glob("*.want"), *root.glob("*.want.zst")]:
        m = pat_w.search(p.name)
        if m:
            ts_set.add(int(m.group(1)))
    return sorted(ts_set)


def _find_peer(path: Path, peer_ext: str) -> Optional[Path]:
    """Given a .got[.zst] find the matching .want[.zst] or vice versa."""
    if path.suffix == ".zst":
        base = path.with_suffix("")
    else:
        base = path
    peer = base.with_suffix(peer_ext)
    if peer.is_file():
        return peer
    peer_zst = Path(str(peer) + ".zst")
    if peer_zst.is_file():
        return peer_zst
    return None


def find_comparable_files_at_timestep(
    timestep: int, root: Path
) -> List[Tuple[Path, Path]]:
    pairs: List[Tuple[Path, Path]] = []
    seen_bases: set[str] = set()

    for want in [*root.glob("*.want"), *root.glob("*.want.zst")]:
        base_name = want.name.removesuffix(".zst")
        if not base_name.endswith(f"_{timestep}.want"):
            continue
        if base_name in seen_bases:
            continue
        seen_bases.add(base_name)

        got = _find_peer(want, ".got")
        if got is None:
            print(f"Skipping {want}: matching .got not found")
            continue
        pairs.append((got, want))

    return pairs


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _stream_lines(path: Path) -> Iterable[str]:
    if path.suffix == ".zst":
        with open(path, "rb") as fbin:
            text = zstd.ZstdDecompressor().decompress(fbin.read()).decode("utf-8", errors="replace")
        for line in text.splitlines():
            ls = line.rstrip("\n\r")
            if not ls.strip():
                continue
            yield ls
    else:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                ls = line.rstrip("\n\r")
                if not ls.strip():
                    continue
                yield ls


KNOWN_METADATA = {"assoc", "alloc", "rank", "size", "lbound", "entries", "missing"}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare_data(
    got: np.ndarray,
    want: np.ndarray,
    abs_tol: float = 1e-12,
    rel_tol: float = 1e-12,
) -> Dict[str, Any]:
    if got.size != want.size:
        return {
            "ok": False, "max_abs": -1.0, "max_rel": -1.0, "mae": -1.0,
            "rmse": -1.0, "SNR_db": -1.0, "vSNR_db": -1.0,
            "mismatches": abs(got.size - want.size),
            "count": max(got.size, want.size),
        }

    g = got.astype(np.float64)
    w = want.astype(np.float64)

    abs_diff = np.abs(g - w)
    scale = np.fmax(np.fmax(np.abs(g), np.abs(w)), abs_tol)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel_diff = abs_diff / scale

    is_close = abs_diff <= np.maximum(
        rel_tol * np.maximum(np.abs(g), np.abs(w)), abs_tol
    )
    mismatches = np.count_nonzero(~is_close)

    norm_diff = np.linalg.norm(abs_diff)
    norm_want = np.linalg.norm(w)

    if norm_diff == 0:
        snr_db = float("inf")
    elif norm_want == 0:
        snr_db = -float("inf")
    else:
        snr_db = 20 * np.log10(norm_want / norm_diff)

    var_want = np.var(w)
    var_err = np.var(g - w)
    if var_err == 0:
        vsnr_db = float("inf")
    elif var_want == 0:
        vsnr_db = -float("inf")
    else:
        vsnr_db = 10 * np.log10(var_want / var_err)

    return {
        "ok": mismatches == 0,
        "max_abs": float(np.max(abs_diff)),
        "max_rel": float(np.max(rel_diff)),
        "mae": float(np.mean(abs_diff)),
        "rmse": float(norm_diff / np.sqrt(g.size)) if g.size > 0 else 0.0,
        "SNR_db": float(snr_db),
        "vSNR_db": float(vsnr_db),
        "mismatches": int(mismatches),
        "count": int(g.size),
    }


def compare_pair(
    got: Path,
    want: Path,
    abs_tol: float = 1e-12,
    rel_tol: float = 1e-12,
    verbose: bool = True,
) -> List[Dict[str, Any]]:
    """Compare a .got/.want pair. Returns list of per-subfield result dicts."""
    if verbose:
        print(f"Comparing {got.name} vs. {want.name}")

    # Derive field name from filename: strip _<timestep>.got[.zst]
    base = got.name.removesuffix(".zst")
    field = re.sub(r"_\d+\.got$", "", base)

    current_var: str = field
    got_accum: List[float] = []
    want_accum: List[float] = []
    results: List[Dict[str, Any]] = []

    def flush_var(var_name: str, g_list: List[float], w_list: List[float]):
        if not g_list:
            return
        g = np.array(g_list)
        w = np.array(w_list)
        stats = compare_data(g, w, abs_tol=abs_tol, rel_tol=rel_tol)
        sf = var_name if var_name != field else "-"
        results.append({
            "field": field,
            "sub_field": sf,
            "status": "OK" if stats["ok"] else "DIFF",
            "max_abs": stats["max_abs"],
            "max_rel": stats["max_rel"],
            "mae": stats["mae"],
            "rmse": stats["rmse"],
            "SNR_db": stats["SNR_db"],
            "vSNR_db": stats["vSNR_db"],
            "mismatches": stats["mismatches"],
            "total_elements": stats["count"],
        })

    for got_line, want_line in zip_longest(_stream_lines(got), _stream_lines(want)):
        if got_line is None or want_line is None:
            flush_var(current_var, got_accum, want_accum)
            results.append({
                "field": field, "sub_field": "-", "status": "ERROR",
                "max_abs": -1.0, "max_rel": -1.0, "mae": -1.0, "rmse": -1.0,
                "SNR_db": -1.0, "vSNR_db": -1.0, "mismatches": -1, "total_elements": -1,
            })
            if verbose:
                print(f"  {field}: Different number of lines")
            return results

        if got_line.startswith("# ") or want_line.startswith("# "):
            if got_line != want_line:
                flush_var(current_var, got_accum, want_accum)
                results.append({
                    "field": field, "sub_field": "-", "status": "ERROR",
                    "max_abs": -1.0, "max_rel": -1.0, "mae": -1.0, "rmse": -1.0,
                    "SNR_db": -1.0, "vSNR_db": -1.0, "mismatches": -1, "total_elements": -1,
                })
                if verbose:
                    print(f"  {field}: Structure mismatch ({got_line} vs. {want_line})")
                return results

            flush_var(current_var, got_accum, want_accum)
            got_accum, want_accum = [], []

            tag = got_line.lstrip("# ").strip().split()[0]
            if tag and tag not in KNOWN_METADATA:
                current_var = tag
            continue

        try:
            got_accum.append(float(got_line))
            want_accum.append(float(want_line))
        except ValueError:
            if verbose:
                print(f"  {field}/{current_var}: Non-numeric data")
            return results

    flush_var(current_var, got_accum, want_accum)
    return results


def compare_timestep_worker(
    ts: int, root: Path, atol: float, rtol: float
) -> Tuple[int, List[Dict[str, Any]]]:
    """Worker: compare all got/want pairs for one timestep."""
    fpairs = find_comparable_files_at_timestep(ts, root)
    all_rows: List[Dict[str, Any]] = []
    for got, want in fpairs:
        rows = compare_pair(got, want, abs_tol=atol, rel_tol=rtol)
        for r in rows:
            r["timestep"] = ts
        all_rows.extend(rows)

        # Print summary per file
        diffs = [r for r in rows if r["status"] == "DIFF"]
        if diffs:
            for r in diffs:
                name = r["field"] if r["sub_field"] == "-" else f"{r['field']} % {r['sub_field']}"
                print(f"  {name}: DIFF | mismatches={r['mismatches']}/{r['total_elements']}"
                      f" | rmse={r['rmse']:.2e} | SNR={r['SNR_db']:.1f}dB")
        elif rows:
            print(f"  {rows[0]['field']}: OK")

    sys.stdout.flush()
    return ts, all_rows


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(conn: sqlite3.Connection):
    try:
        import polars as pl
    except ImportError:
        print("(install polars for a formatted summary table)")
        return

    rows = conn.execute(
        "SELECT timestep, field, sub_field, status, "
        "max_abs, max_rel, mae, rmse, SNR_db, vSNR_db, mismatches, total_elements "
        "FROM comparisons ORDER BY timestep, field, sub_field"
    ).fetchall()

    if not rows:
        print("No results yet.")
        return

    df = pl.DataFrame(rows, schema=[
        "timestep", "field", "sub_field", "status",
        "max_abs", "max_rel", "mae", "rmse", "SNR_db", "vSNR_db",
        "mismatches", "total_elements",
    ], orient="row")

    def fmt_count(n: int) -> str:
        if n < 0:
            return "?"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    display = (
        df.with_columns(
            pl.when(pl.col("sub_field") == "-")
            .then(pl.col("field"))
            .otherwise(pl.col("field") + " % " + pl.col("sub_field"))
            .alias("name"),
            pl.col("total_elements").map_elements(fmt_count, return_dtype=pl.Utf8).alias("N"),
            pl.col("mismatches").map_elements(fmt_count, return_dtype=pl.Utf8).alias("mis-\nmatch"),
        )
        .select([
            "timestep", "name", "status",
            "max_abs", "max_rel", "mae", "rmse",
            "SNR_db", "vSNR_db",
            "mis-\nmatch", "N",
        ])
        .rename({
            "max_abs": "max\nabs",
            "max_rel": "max\nrel",
            "SNR_db": "SNR\n(dB)",
            "vSNR_db": "vSNR\n(dB)",
        })
        .sort(["timestep", "name"])
    )

    print("\n" + "=" * 100)
    print("GOT vs WANT COMPARISON SUMMARY")
    print("=" * 100)
    with pl.Config(tbl_rows=500, tbl_width_chars=220, tbl_cols=-1, float_precision=2):
        if display["timestep"].n_unique() == 1:
            print(display.drop("timestep"))
        else:
            print(display)
    print("=" * 100)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    argp = argparse.ArgumentParser(
        description="Compare got vs want files, store results in SQLite."
    )
    argp.add_argument(
        "-r", "--root", default=".",
        help="Directory containing .got/.want files (default: cwd)",
    )
    argp.add_argument(
        "timesteps", type=str, nargs="?", default="",
        help="Comma-separated timesteps. If omitted, all discovered timesteps.",
    )
    argp.add_argument("-o", "--output", default=None,
                      help="SQLite output path (default: <root>/gotwant.db)")
    argp.add_argument("--atol", type=float, default=1e-12, help="Absolute tolerance")
    argp.add_argument("--rtol", type=float, default=1e-12, help="Relative tolerance")
    argp.add_argument("-j", "--workers", type=int, default=DEFAULT_WORKERS)
    argp.add_argument("--force", action="store_true",
                      help="Re-compare timesteps already in the DB")
    args = argp.parse_args()

    root = Path(args.root)
    db_path = Path(args.output) if args.output else root / "gotwant.db"

    timesteps = [ts.strip() for ts in args.timesteps.split(",") if ts.strip()]
    assert all(ts.isdigit() for ts in timesteps), "Timesteps must be integers"
    timesteps = [int(ts) for ts in timesteps]
    if not timesteps:
        timesteps = discover_timesteps(root)

    if not timesteps:
        print(f"No .got/.want files found in {root}")
        return

    conn = init_db(db_path)

    if not args.force:
        before = len(timesteps)
        timesteps = [ts for ts in timesteps if not timestep_exists(conn, ts)]
        skipped = before - len(timesteps)
        if skipped:
            print(f"Skipped {skipped} timesteps already in DB (use --force to re-run)")

    if not timesteps:
        print("All timesteps already computed. Nothing to do.")
        print_summary(conn)
        conn.close()
        return

    print(f"Root:      {root}")
    print(f"DB:        {db_path}")
    print(f"Timesteps: {timesteps}")
    print(f"Workers:   {args.workers}\n")

    worker = partial(compare_timestep_worker, root=root, atol=args.atol, rtol=args.rtol)

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for ts, rows in ex.map(worker, timesteps):
            if rows:
                insert_rows(conn, rows)

    print_summary(conn)
    conn.close()
    print(f"\nResults in: {db_path}")


if __name__ == "__main__":
    main()
