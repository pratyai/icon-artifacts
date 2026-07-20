"""Compare serialized ICON .data files — both within-dir (convergence) and cross-dir.

Modes:
    # Cross-directory: ref vs test at matching (phys, field, vt, ss)
    python utils/compare_serde.py cross REF_DIR TEST_DIR --grid R02B04 --tag OG_vs_F32 -o all.db

    # Within-directory: pairwise ss combinations
    python utils/compare_serde.py convergence DIR --grid R02B04 --tag OG -o all.db

All results go into one unified DB.
"""

import argparse
import itertools
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

# Each worker holds a full field for both runs, so peak memory scales with the
# worker count times the grid size. The cap keeps the default survivable on the
# finest grids, where an unbounded count exhausts node memory and the pool is
# OOM-killed; raise it with `-j` on coarse grids where the fields are small.
MAX_DEFAULT_WORKERS = 16

DEFAULT_WORKERS = min(
    int(
        os.environ.get(
            "SLURM_CPUS_PER_TASK",
            os.environ.get("SLURM_CPUS_ON_NODE", os.cpu_count() or 1),
        )
    ),
    MAX_DEFAULT_WORKERS,
)

# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS comparisons (
    grid        TEXT    NOT NULL,
    tag         TEXT    NOT NULL,
    phys        INTEGER NOT NULL,
    field       TEXT    NOT NULL,
    sub_field   TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    max_abs     REAL,
    max_rel     REAL,
    mae         REAL,
    rmse        REAL,
    SNR_db      REAL,
    vSNR_db     REAL,
    mismatches  INTEGER,
    total_elements INTEGER,
    PRIMARY KEY (grid, tag, phys, field, sub_field)
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def tag_exists(conn: sqlite3.Connection, grid: str, tag: str, phys: int, field: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM comparisons WHERE grid=? AND tag=? AND phys=? AND field=? LIMIT 1",
        (grid, tag, phys, field),
    ).fetchone()
    return row is not None


def insert_rows(conn: sqlite3.Connection, rows: List[Dict[str, Any]]):
    if not rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO comparisons
           (grid, tag, phys, field, sub_field, status,
            max_abs, max_rel, mae, rmse, SNR_db, vSNR_db,
            mismatches, total_elements)
           VALUES (:grid, :tag, :phys, :field, :sub_field, :status,
                   :max_abs, :max_rel, :mae, :rmse, :SNR_db, :vSNR_db,
                   :mismatches, :total_elements)""",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

METADATA_TAGS = {"assoc", "alloc", "rank", "size", "lbound", "entries", "missing"}
_TAG_RE = re.compile(r"^#\s+(\S+)", re.MULTILINE)


def read_text(path: Path) -> str:
    if path.suffix == ".zst":
        with open(path, "rb") as f:
            return zstd.ZstdDecompressor().decompress(f.read()).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


# Fortran list-directed output drops the exponent letter once the exponent
# needs three digits, writing 1.0E-112 as "1.0-112". Values that small only
# survive in formats with a wide exponent range, so bf16 dumps hit this where
# fp16 ones have already flushed to zero.
_FORTRAN_EXP_RE = re.compile(r"(?<=[0-9.])([+-])(\d{3,})(?=\s|$)")


def _fast_parse_floats(block: str) -> np.ndarray:
    """Parse a block of newline-separated floats ~5-10x faster than np.fromstring."""
    lines = block.split()
    if not lines:
        return np.empty(0, dtype=np.float64)
    try:
        return np.array(lines, dtype=np.float64)
    except ValueError:
        repaired = [_FORTRAN_EXP_RE.sub(r"E\1\2", tok) for tok in lines]
        return np.array(repaired, dtype=np.float64)


def _parse_text(text: str) -> Dict[str, np.ndarray]:
    results: Dict[str, np.ndarray] = {}
    tags = []
    for m in _TAG_RE.finditer(text):
        eol = text.find("\n", m.end())
        data_start = eol + 1 if eol != -1 else len(text)
        tags.append((m.start(), m.group(1), data_start))

    if not tags:
        arr = _fast_parse_floats(text)
        if arr.size > 0:
            results["root"] = arr
        return results

    current_field: Optional[str] = None
    for i, (_, tag_name, data_start) in enumerate(tags):
        data_end = tags[i + 1][0] if i + 1 < len(tags) else len(text)
        if tag_name == "entries":
            if current_field is not None:
                arr = _fast_parse_floats(text[data_start:data_end])
                if arr.size > 0:
                    if current_field in results:
                        results[current_field] = np.concatenate([results[current_field], arr])
                    else:
                        results[current_field] = arr
        elif tag_name not in METADATA_TAGS:
            current_field = tag_name
    return results


def parse_serde_file(path: Path) -> Dict[str, np.ndarray]:
    return _parse_text(read_text(path))


def count_entries_from_parsed(fields: Dict[str, np.ndarray]) -> int:
    return sum(a.size for a in fields.values())


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
    """Return {(phys, field, ss): path}, filtering to d=1, vt=1 only."""
    lookup: Dict[Tuple[int, str, int], Path] = {}
    for f in d.iterdir():
        m = FILE_PAT.match(f.name)
        if m:
            if int(m.group("d")) != 1:
                continue
            if int(m.group("vt")) != 1:
                continue
            field = m.group("field")
            if field.split(".")[0] in SKIP_FIELDS:
                continue
            key = (int(m.group("phys")), field, int(m.group("ss")))
            lookup[key] = f
    return lookup


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def _compare_pair(
    path_a: Path, path_b: Path, atol: float, rtol: float,
) -> List[Dict[str, Any]]:
    """Compare two serde files, return list of sub-field result dicts."""
    a_fields = parse_serde_file(path_a)
    b_fields = parse_serde_file(path_b)

    # Fast path: check if all arrays are identical without full stats
    if all(
        k in b_fields and a_fields[k].shape == b_fields[k].shape
        and np.array_equal(a_fields[k], b_fields[k])
        for k in a_fields
    ) and a_fields.keys() == b_fields.keys():
        total = count_entries_from_parsed(a_fields)
        return [{
            "sub_field": "-", "status": "OK",
            "max_abs": 0.0, "max_rel": 0.0, "mae": 0.0, "rmse": 0.0,
            "SNR_db": float("inf"), "vSNR_db": float("inf"),
            "mismatches": 0, "total_elements": total,
        }]
    all_vars = sorted(set(a_fields) | set(b_fields))
    has_subfields = len(all_vars) > 1

    rows = []
    for var in all_vars:
        sf = var if has_subfields else "-"
        if var not in a_fields or var not in b_fields:
            rows.append({
                "sub_field": sf, "status": "MISSING",
                "max_abs": -1.0, "max_rel": -1.0, "mae": -1.0, "rmse": -1.0,
                "SNR_db": -1.0, "vSNR_db": -1.0,
                "mismatches": -1, "total_elements": -1,
            })
            continue
        stats = compare_data(a_fields[var], b_fields[var], abs_tol=atol, rel_tol=rtol)
        rows.append({
            "sub_field": sf,
            "status": "OK" if stats["ok"] else "DIFF",
            "max_abs": stats["max_abs"], "max_rel": stats["max_rel"],
            "mae": stats["mae"], "rmse": stats["rmse"],
            "SNR_db": stats["SNR_db"], "vSNR_db": stats["vSNR_db"],
            "mismatches": stats["mismatches"], "total_elements": stats["count"],
        })
    return rows


def cross_worker(task, atol, rtol):
    grid, tag, phys, field, path_a, path_b = task
    subs = _compare_pair(path_a, path_b, atol, rtol)
    return [{"grid": grid, "tag": tag, "phys": phys, "field": field, **s} for s in subs]


def convergence_worker(task, atol, rtol):
    grid, phys, field, ss_paths, pairs = task
    # Read and parse all ss files once
    parsed: Dict[int, Dict[str, np.ndarray]] = {}
    for ss, path in ss_paths.items():
        parsed[ss] = parse_serde_file(path)

    results = []
    for ss_a, ss_b in pairs:
        tag = f"ss{ss_a}_vs_ss{ss_b}"

        a_f, b_f = parsed[ss_a], parsed[ss_b]
        if (a_f.keys() == b_f.keys() and all(
            a_f[k].shape == b_f[k].shape and np.array_equal(a_f[k], b_f[k])
            for k in a_f
        )):
            total = count_entries_from_parsed(a_f)
            results.append({
                "grid": grid, "tag": tag, "phys": phys, "field": field,
                "sub_field": "-", "status": "OK",
                "max_abs": 0.0, "max_rel": 0.0, "mae": 0.0, "rmse": 0.0,
                "SNR_db": float("inf"), "vSNR_db": float("inf"),
                "mismatches": 0, "total_elements": total,
            })
            continue

        a_fields = parsed[ss_a]
        b_fields = parsed[ss_b]
        all_vars = sorted(set(a_fields) | set(b_fields))
        has_subfields = len(all_vars) > 1

        for var in all_vars:
            sf = var if has_subfields else "-"
            if var not in a_fields or var not in b_fields:
                results.append({
                    "grid": grid, "tag": tag, "phys": phys, "field": field,
                    "sub_field": sf, "status": "MISSING",
                    "max_abs": -1.0, "max_rel": -1.0, "mae": -1.0, "rmse": -1.0,
                    "SNR_db": -1.0, "vSNR_db": -1.0,
                    "mismatches": -1, "total_elements": -1,
                })
                continue
            stats = compare_data(a_fields[var], b_fields[var], abs_tol=atol, rel_tol=rtol)
            results.append({
                "grid": grid, "tag": tag, "phys": phys, "field": field,
                "sub_field": sf,
                "status": "OK" if stats["ok"] else "DIFF",
                "max_abs": stats["max_abs"], "max_rel": stats["max_rel"],
                "mae": stats["mae"], "rmse": stats["rmse"],
                "SNR_db": stats["SNR_db"], "vSNR_db": stats["vSNR_db"],
                "mismatches": stats["mismatches"], "total_elements": stats["count"],
            })
    return results


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def run_cross(args, conn: sqlite3.Connection):
    ref_lookup = discover_files(Path(args.ref))
    test_lookup = discover_files(Path(args.test))

    # Match on (phys, field) only — ss may differ between ref and test.
    ref_by_pf: Dict[Tuple[int, str], Path] = {
        (phys, field): path for (phys, field, _), path in ref_lookup.items()
    }
    test_by_pf: Dict[Tuple[int, str], Path] = {
        (phys, field): path for (phys, field, _), path in test_lookup.items()
    }

    common = sorted(set(ref_by_pf) & set(test_by_pf))
    if args.phys:
        common = [k for k in common if k[0] in args.phys]

    tasks = []
    skipped = 0
    for phys, field in common:
        if not args.force and tag_exists(conn, args.grid, args.tag, phys, field):
            skipped += 1
            continue
        tasks.append((args.grid, args.tag, phys, field,
                       ref_by_pf[(phys, field)], test_by_pf[(phys, field)]))

    print(f"Ref:     {args.ref}")
    print(f"Test:    {args.test}")
    print(f"Grid:    {args.grid}  Tag: {args.tag}")
    print(f"Matched: {len(common)}")
    if skipped:
        print(f"Skipped: {skipped}")
    print(f"To do:   {len(tasks)}")

    if not tasks:
        return 0

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(partial(cross_worker, atol=args.atol, rtol=args.rtol), t): t
            for t in tasks
        }
        return _drain(conn, futures, desc=f"{args.grid}/{args.tag}")


def run_convergence(args, conn: sqlite3.Connection):
    lookup = discover_files(Path(args.dir))
    all_ss = sorted(set(k[2] for k in lookup))
    all_phys = sorted(set(k[0] for k in lookup))
    phys_steps = args.phys if args.phys else all_phys
    ss_pairs = list(itertools.combinations(all_ss, 2))

    print(f"Dir:     {args.dir}")
    print(f"Grid:    {args.grid}")
    print(f"SS:      {all_ss}")
    print(f"Pairs:   {len(ss_pairs)} per (phys, field)")

    tasks = []
    skipped = 0
    for phys in phys_steps:
        fields_at_phys = sorted(set(
            k[1] for k in lookup if k[0] == phys
        ))
        for field in fields_at_phys:
            ss_paths = {ss: lookup[(phys, field, ss)]
                        for ss in all_ss if (phys, field, ss) in lookup}
            pairs_needed = []
            for ss_a, ss_b in ss_pairs:
                if ss_a not in ss_paths or ss_b not in ss_paths:
                    continue
                tag = f"ss{ss_a}_vs_ss{ss_b}"
                if not args.force and tag_exists(conn, args.grid, tag, phys, field):
                    skipped += 1
                    continue
                pairs_needed.append((ss_a, ss_b))
            if pairs_needed:
                tasks.append((args.grid, phys, field, ss_paths, pairs_needed))

    total = sum(len(t[4]) for t in tasks)
    if skipped:
        print(f"Skipped: {skipped}")
    print(f"To do:   {total} pairs in {len(tasks)} groups")

    if not tasks:
        return 0

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(partial(convergence_worker, atol=args.atol, rtol=args.rtol), t): t
            for t in tasks
        }
        return _drain(conn, futures, desc=f"{args.grid}/convergence")


def _drain(conn, futures, desc=""):
    """Collect worker results into the DB. Returns the number that failed.

    A failure here means the comparison it covered is absent from the DB, so
    callers must not treat a partially-drained run as a complete one.
    """
    pbar = tqdm(total=len(futures), desc=desc, unit="task")
    batch = []
    failed = 0
    for fut in futures:
        pbar.update(1)
        try:
            rows = fut.result()
            batch.extend(rows)
        except Exception as e:
            failed += 1
            tqdm.write(f"Error: {e}")
        if len(batch) >= 200:
            insert_rows(conn, batch)
            batch = []
    if batch:
        insert_rows(conn, batch)
    pbar.close()
    return failed


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(conn: sqlite3.Connection, grid: str = None, tag: str = None,
                   phys: list[int] | None = None):
    try:
        import polars as pl
    except ImportError:
        print("(install polars for summary table)")
        return

    where, params = [], []
    if grid:
        where.append("grid = ?")
        params.append(grid)
    if tag:
        where.append("tag = ?")
        params.append(tag)
    if phys:
        where.append(f"phys IN ({','.join('?' for _ in phys)})")
        params.extend(phys)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT grid, tag, phys, field, sub_field, status, "
        "SNR_db, vSNR_db, mismatches, total_elements "
        f"FROM comparisons{clause} ORDER BY grid, tag, phys, field, sub_field",
        params,
    ).fetchall()

    if not rows:
        print("No results yet.")
        return

    df = pl.DataFrame(rows, schema=[
        "grid", "tag", "phys", "field", "sub_field", "status",
        "SNR_db", "vSNR_db", "mismatches", "total_elements",
    ], orient="row")

    display = (
        df.with_columns(
            pl.when(pl.col("sub_field") == "-")
            .then(pl.col("field"))
            .otherwise(pl.col("field") + " % " + pl.col("sub_field"))
            .alias("name"),
        )
        .select(["grid", "tag", "phys", "name", "status",
                 "SNR_db", "vSNR_db", "mismatches", "total_elements"])
        .rename({"SNR_db": "SNR (dB)", "vSNR_db": "vSNR (dB)"})
        .sort(["grid", "tag", "phys", "name"])
    )

    print(f"\n{'=' * 100}")
    print("COMPARISON SUMMARY")
    print("=" * 100)
    with pl.Config(tbl_rows=500, tbl_width_chars=220, tbl_cols=-1, float_precision=2):
        print(display)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Compare ICON serde .data files (cross-dir or within-dir convergence)."
    )
    parser.add_argument("-o", "--output", required=True, help="SQLite output path (shared DB)")
    parser.add_argument("-j", "--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--atol", type=float, default=1e-12)
    parser.add_argument("--rtol", type=float, default=1e-12)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--fresh", action="store_true", help="Delete DB before starting")

    sub = parser.add_subparsers(dest="mode", required=True)

    # Cross mode
    p_cross = sub.add_parser("cross", help="Compare ref dir vs test dir")
    p_cross.add_argument("ref", help="Reference directory (e.g. OG FP64)")
    p_cross.add_argument("test", help="Test directory (e.g. NU F32)")
    p_cross.add_argument("--grid", required=True, help="Grid label (e.g. R02B04)")
    p_cross.add_argument("--tag", required=True, help="Comparison tag (e.g. OG_vs_F32)")
    p_cross.add_argument("-p", "--phys", type=int, nargs="*", default=None)

    # Convergence mode
    p_conv = sub.add_parser("convergence", help="Pairwise ss comparisons within one dir")
    p_conv.add_argument("dir", help="Directory with .data files at multiple ss values")
    p_conv.add_argument("--grid", required=True, help="Grid label")
    p_conv.add_argument("-p", "--phys", type=int, nargs="*", default=None)

    args = parser.parse_args()

    db_path = Path(args.output)
    if args.fresh and db_path.exists():
        db_path.unlink()
    conn = init_db(db_path)

    print(f"DB: {db_path}")
    print(f"Workers: {args.workers}\n")

    failed = 0
    if args.mode == "cross":
        failed = run_cross(args, conn)
        print_summary(conn, grid=args.grid, tag=args.tag, phys=args.phys)
    elif args.mode == "convergence":
        failed = run_convergence(args, conn)
        print_summary(conn, grid=args.grid, phys=args.phys)
    conn.close()
    print(f"\nResults in: {db_path}")

    if failed:
        # The DB is missing whatever those tasks covered. Workers dying en
        # masse usually means the pool was OOM-killed — retry with a smaller
        # `-j`. Exiting non-zero keeps a truncated DB from passing for a
        # complete one.
        print(f"\n{failed} comparison(s) failed — results are INCOMPLETE.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
