"""Compare serialized ICON .data files across two directories (ref vs test).

Usage:
    python utils/compare_cross.py REF_DIR TEST_DIR -o cross.db
"""

import argparse
import filecmp
import os
import re
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import zstandard as zstd
from tqdm import tqdm

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
    phys        INTEGER NOT NULL,
    field       TEXT    NOT NULL,
    sub_field   TEXT    NOT NULL,
    ss          INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    max_abs     REAL,
    max_rel     REAL,
    mae         REAL,
    rmse        REAL,
    SNR_db      REAL,
    vSNR_db     REAL,
    mismatches  INTEGER,
    total_elements INTEGER,
    PRIMARY KEY (phys, field, sub_field, ss)
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def already_done(conn: sqlite3.Connection, phys: int, field: str, ss: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM comparisons WHERE phys=? AND field=? AND ss=? LIMIT 1",
        (phys, field, ss),
    ).fetchone()
    return row is not None


def insert_rows(conn: sqlite3.Connection, rows: List[Dict[str, Any]]):
    if not rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO comparisons
           (phys, field, sub_field, ss, status,
            max_abs, max_rel, mae, rmse, SNR_db, vSNR_db,
            mismatches, total_elements)
           VALUES (:phys, :field, :sub_field, :ss, :status,
                   :max_abs, :max_rel, :mae, :rmse, :SNR_db, :vSNR_db,
                   :mismatches, :total_elements)""",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# File I/O (same serde format as v3)
# ---------------------------------------------------------------------------

METADATA_TAGS = {"assoc", "alloc", "rank", "size", "lbound", "entries", "missing"}
_TAG_RE = re.compile(r"^#\s+(\S+)", re.MULTILINE)


def read_text(path: Path) -> str:
    if path.suffix == ".zst":
        with open(path, "rb") as f:
            return zstd.ZstdDecompressor().decompress(f.read()).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def parse_serde_file(path: Path) -> Dict[str, np.ndarray]:
    text = read_text(path)
    return _parse_text(text)


def _parse_text(text: str) -> Dict[str, np.ndarray]:
    results: Dict[str, np.ndarray] = {}
    tags = []
    for m in _TAG_RE.finditer(text):
        eol = text.find("\n", m.end())
        data_start = eol + 1 if eol != -1 else len(text)
        tags.append((m.start(), m.group(1), data_start))

    if not tags:
        try:
            arr = np.fromstring(text, sep="\n", dtype=np.float64)
            if arr.size > 0:
                results["root"] = arr
        except ValueError:
            pass
        return results

    current_field: Optional[str] = None
    for i, (_, tag_name, data_start) in enumerate(tags):
        data_end = tags[i + 1][0] if i + 1 < len(tags) else len(text)
        if tag_name == "entries":
            if current_field is not None:
                block = text[data_start:data_end]
                arr = np.fromstring(block, sep="\n", dtype=np.float64)
                if arr.size > 0:
                    if current_field in results:
                        results[current_field] = np.concatenate([results[current_field], arr])
                    else:
                        results[current_field] = arr
        elif tag_name not in METADATA_TAGS:
            current_field = tag_name
    return results


def count_entries(path: Path) -> int:
    text = read_text(path)
    count = 0
    in_entries = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            tag = stripped[2:].strip().split()[0]
            in_entries = tag == "entries"
        elif in_entries and stripped:
            count += 1
    return count


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


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

SKIP_FIELDS = {"dtime", "istep", "ntnd", "lvn_only", "ldeepatmo", "dt_linintp_ubc",
               "global_data", "p_patch", "p_int",
               "z_kin_hor_e", "z_vt_ie", "z_w_concorr_me"}

FILE_PAT = re.compile(
    r"^(?P<field>.+?)\.t0\.p(?P<phys>\d+)\.d(?P<d>\d+)\.vt(?P<vt>\d+)\.ss(?P<ss>\d+)\.data(?:\.zst)?$"
)


def discover_files(d: Path) -> Dict[Tuple[int, str, int], Path]:
    """Return {(phys, field, ss): path} for all matching files."""
    lookup: Dict[Tuple[int, str, int], Path] = {}
    for f in d.iterdir():
        m = FILE_PAT.match(f.name)
        if m:
            field = m.group("field")
            if field.split(".")[0] in SKIP_FIELDS:
                continue
            key = (int(m.group("phys")), field, int(m.group("ss")))
            lookup[key] = f
    return lookup


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def compare_worker(
    task: Tuple[int, str, int, Path, Path],
    atol: float,
    rtol: float,
) -> List[Dict[str, Any]]:
    phys, field, ss, ref_path, test_path = task

    # Fast path: byte-identical
    if filecmp.cmp(str(ref_path), str(test_path), shallow=False):
        total = count_entries(ref_path)
        return [{
            "phys": phys, "field": field, "sub_field": "-", "ss": ss,
            "status": "OK",
            "max_abs": 0.0, "max_rel": 0.0, "mae": 0.0, "rmse": 0.0,
            "SNR_db": float("inf"), "vSNR_db": float("inf"),
            "mismatches": 0, "total_elements": total,
        }]

    ref_fields = parse_serde_file(ref_path)
    test_fields = parse_serde_file(test_path)

    rows = []
    all_vars = sorted(set(ref_fields) | set(test_fields))
    has_subfields = len(all_vars) > 1

    for var in all_vars:
        sf = var if has_subfields else "-"
        if var not in ref_fields or var not in test_fields:
            rows.append({
                "phys": phys, "field": field, "sub_field": sf, "ss": ss,
                "status": "MISSING",
                "max_abs": -1.0, "max_rel": -1.0, "mae": -1.0, "rmse": -1.0,
                "SNR_db": -1.0, "vSNR_db": -1.0,
                "mismatches": -1, "total_elements": -1,
            })
            continue

        stats = compare_data(ref_fields[var], test_fields[var], abs_tol=atol, rel_tol=rtol)
        rows.append({
            "phys": phys, "field": field, "sub_field": sf, "ss": ss,
            "status": "OK" if stats["ok"] else "DIFF",
            "max_abs": stats["max_abs"], "max_rel": stats["max_rel"],
            "mae": stats["mae"], "rmse": stats["rmse"],
            "SNR_db": stats["SNR_db"], "vSNR_db": stats["vSNR_db"],
            "mismatches": stats["mismatches"], "total_elements": stats["count"],
        })

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Compare matching serde .data files across two directories (ref vs test)."
    )
    parser.add_argument("ref", help="Reference directory (e.g. OG ICON FP64)")
    parser.add_argument("test", help="Test directory (e.g. NU ICON F32/F16)")
    parser.add_argument("-o", "--output", required=True, help="SQLite output path")
    parser.add_argument("-p", "--phys", type=int, nargs="*", default=None,
                        help="Physics steps to compare (default: all)")
    parser.add_argument("--atol", type=float, default=1e-12)
    parser.add_argument("--rtol", type=float, default=1e-12)
    parser.add_argument("-j", "--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--force", action="store_true", help="Recompute existing entries")
    args = parser.parse_args()

    ref_dir = Path(args.ref)
    test_dir = Path(args.test)
    db_path = Path(args.output)

    ref_lookup = discover_files(ref_dir)
    test_lookup = discover_files(test_dir)

    # Find matching (phys, field, ss) keys
    common_keys = sorted(set(ref_lookup) & set(test_lookup))
    if args.phys:
        common_keys = [k for k in common_keys if k[0] in args.phys]

    if not common_keys:
        print(f"No matching files between {ref_dir} and {test_dir}")
        return

    conn = init_db(db_path)

    # Filter out already-done
    tasks = []
    skipped = 0
    for phys, field, ss in common_keys:
        if not args.force and already_done(conn, phys, field, ss):
            skipped += 1
            continue
        tasks.append((phys, field, ss, ref_lookup[(phys, field, ss)],
                       test_lookup[(phys, field, ss)]))

    print(f"Ref:     {ref_dir}")
    print(f"Test:    {test_dir}")
    print(f"DB:      {db_path}")
    print(f"Matched: {len(common_keys)} (phys, field, ss) keys")
    if skipped:
        print(f"Skipped: {skipped} already in DB")
    print(f"To do:   {len(tasks)}")
    print(f"Workers: {args.workers}")

    if not tasks:
        print("Nothing to do.")
    else:
        print()
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(partial(compare_worker, atol=args.atol, rtol=args.rtol), t): t
                for t in tasks
            }
            pbar = tqdm(total=len(futures), desc="Comparing", unit="file")
            batch = []
            for fut in futures:
                _, field, _, _, _ = futures[fut]
                pbar.set_postfix_str(field, refresh=True)
                try:
                    rows = fut.result()
                    batch.extend(rows)
                except Exception as e:
                    tqdm.write(f"Error: {field}: {e}")
                pbar.update(1)
                if len(batch) >= 200:
                    insert_rows(conn, batch)
                    batch = []
            if batch:
                insert_rows(conn, batch)
            pbar.close()

    # Summary
    print_summary(conn)
    conn.close()
    print(f"\nResults in: {db_path}")


def print_summary(conn: sqlite3.Connection):
    try:
        import polars as pl
    except ImportError:
        print("(install polars for summary table)")
        return

    rows = conn.execute(
        "SELECT phys, field, sub_field, ss, status, "
        "max_abs, max_rel, rmse, SNR_db, vSNR_db, mismatches, total_elements "
        "FROM comparisons ORDER BY phys, field, sub_field, ss"
    ).fetchall()

    if not rows:
        print("No results yet.")
        return

    df = pl.DataFrame(rows, schema=[
        "phys", "field", "sub_field", "ss", "status",
        "max_abs", "max_rel", "rmse", "SNR_db", "vSNR_db",
        "mismatches", "total_elements",
    ], orient="row")

    display = (
        df.with_columns(
            pl.when(pl.col("sub_field") == "-")
            .then(pl.col("field"))
            .otherwise(pl.col("field") + " % " + pl.col("sub_field"))
            .alias("name"),
        )
        .select([
            "phys", "name", "ss", "status",
            "SNR_db", "vSNR_db",
            "mismatches", "total_elements",
        ])
        .rename({"SNR_db": "SNR (dB)", "vSNR_db": "vSNR (dB)"})
        .sort(["phys", "name", "ss"])
    )

    print(f"\n{'=' * 80}")
    print("CROSS-DIRECTORY COMPARISON SUMMARY")
    print("=" * 80)
    with pl.Config(tbl_rows=500, tbl_width_chars=220, tbl_cols=-1, float_precision=2):
        print(display)


if __name__ == "__main__":
    main()
