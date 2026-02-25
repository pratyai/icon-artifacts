import argparse
import sqlite3
import re
import zstandard as zstd
import numpy as np
import polars as pl
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from typing import Dict, List, Any


# Standardized comparison logic
def compare_data(
    got: np.ndarray,
    want: np.ndarray,
    abs_tol: float = 1e-12,
    rel_tol: float = 1e-12,
) -> Dict[str, Any]:
    if got.size != want.size:
        return {
            "ok": False,
            "max_abs": None,
            "max_rel": None,
            "mae": None,
            "rmse": None,
            "snr_db": None,
            "mismatches": abs(got.size - want.size),
            "count": got.size,
            "has_nan": False,
            "max_norm_want": None,
            "l2_norm_want": None,
            "error": f"SIZE_MISMATCH: {got.size} vs {want.size}",
        }

    # Convert to float64 for stable comparison and to avoid overflow in norms
    got_64 = got.astype(np.float64)
    want_64 = want.astype(np.float64)

    abs_diff = np.abs(got_64 - want_64)
    # Use fmax to ignore NaNs in the scaling factor and ensure it is at least abs_tol
    scale = np.fmax(np.fmax(np.abs(got_64), np.abs(want_64)), abs_tol)
    with np.errstate(divide="ignore", invalid="ignore"):
        rel_diff = abs_diff / scale

    # Mismatch count logic matching math.isclose
    is_close = abs_diff <= np.maximum(
        rel_tol * np.maximum(np.abs(got_64), np.abs(want_64)), abs_tol
    )
    mismatches = np.count_nonzero(~is_close)

    # Use linalg.norm for stability and handle potential overflows in sum of squares
    norm_diff = np.linalg.norm(abs_diff)
    norm_want = np.linalg.norm(want_64)

    if norm_diff == 0:
        snr_db = float("inf")
    elif norm_want == 0:
        snr_db = -float("inf")
    else:
        snr_db = 20 * np.log10(norm_want / norm_diff)

    return {
        "ok": mismatches == 0,
        "max_abs": np.max(abs_diff) if abs_diff.size > 0 else 0.0,
        "max_rel": np.max(rel_diff) if rel_diff.size > 0 else 0.0,
        "mean_abs": np.mean(abs_diff) if abs_diff.size > 0 else 0.0,
        "rmse": norm_diff / np.sqrt(got.size) if got.size > 0 else 0.0,
        "snr_db": snr_db,
        "mismatches": int(mismatches),
        "count": got.size,
        "has_nan": not (np.all(np.isfinite(got_64)) and np.all(np.isfinite(want_64))),
        "max_norm_want": np.max(np.abs(want_64)) if want_64.size > 0 else 0.0,
        "l2_norm_want": norm_want,
        "psnr_db": (
            float("inf")
            if norm_diff == 0
            else -float("inf")
            if np.max(np.abs(want_64)) == 0
            else 20
            * np.log10(np.max(np.abs(want_64)) / (norm_diff / np.sqrt(got.size)))
        ),
    }


# Metadata constants
TEXT_FIELDS = {"global_data", "p_diag", "p_metrics", "p_prog", "p_int", "p_patch"}
KNOWN_METADATA = {"assoc", "rank", "size", "lbound", "entries"}

D_CTX = zstd.ZstdDecompressor()


def decompress(blob: bytes) -> bytes:
    return D_CTX.decompress(blob)


def parse_serialized_text(text: str) -> Dict[str, np.ndarray]:
    results = {}
    # Find all tag lines: lines starting with '# '
    tag_matches = list(re.finditer(r"^#\s+(\w+).*$", text, re.MULTILINE))

    if not tag_matches:
        val = np.fromstring(text, sep=" ")
        if val.size > 0:
            results["root"] = val
        return results

    current_var = "root"
    # Process potential data before the first tag
    first_data = text[: tag_matches[0].start()]
    if first_data.strip():
        results[current_var] = np.fromstring(first_data, sep=" ")

    for i, m in enumerate(tag_matches):
        tag = m.group(1)
        # Text between this tag and the next
        start = m.end()
        end = tag_matches[i + 1].start() if i + 1 < len(tag_matches) else len(text)

        if tag not in KNOWN_METADATA:
            current_var = tag

        data_text = text[start:end]
        if data_text.strip():
            val = np.fromstring(data_text, sep=" ")
            if val.size > 0:
                if current_var in results:
                    results[current_var] = np.concatenate([results[current_var], val])
                else:
                    results[current_var] = val

    return results


_worker_golden_baseline = {}


def init_worker(golden_baseline):
    global _worker_golden_baseline
    _worker_golden_baseline = golden_baseline


def process_row(row, metric, atol, rtol):
    ts, field_name, got_z, want_z, lp, ss = row
    label = f"{lp}_ss{ss}"
    got_raw = decompress(got_z)

    # Use golden baseline if available, fallback to local want
    want_raw = _worker_golden_baseline.get((ts, field_name), decompress(want_z))
    if want_raw is None:
        return []

    got_dict = parse_serialized_text(got_raw.decode("utf-8"))
    want_dict = parse_serialized_text(want_raw.decode("utf-8"))

    results = []

    def get_val(stats):
        m = "snr_db" if metric == "snr" else "psnr_db" if metric == "psnr" else metric
        val = stats.get(m)
        if val is None:
            return stats.get("error", "ERROR")
        try:
            return f"{float(val):.4e}"
        except (ValueError, TypeError):
            return val

    if field_name in TEXT_FIELDS:
        for var in got_dict:
            if var in want_dict:
                stats = compare_data(
                    got_dict[var],
                    want_dict[var],
                    abs_tol=atol,
                    rel_tol=rtol,
                )
                results.append(
                    {
                        "ts": ts,
                        "item": f"{field_name} % {var}",
                        "version": label,
                        "value": get_val(stats),
                    }
                )
    else:
        # Simple arrays store their entries under their own name tag
        # Use the first available tag if "root" isn't present
        tag = field_name if field_name in got_dict else next(iter(got_dict), "root")
        stats = compare_data(
            got_dict.get(tag, np.array([])),
            want_dict.get(tag, np.array([])),
            abs_tol=atol,
            rel_tol=rtol,
        )
        results.append(
            {
                "ts": ts,
                "item": field_name,
                "version": label,
                "value": get_val(stats),
            }
        )
    return results


def get_run_label(lp, ss):
    return f"{lp}_ss{ss}"


def main():
    parser = argparse.ArgumentParser(
        description="Showdown script to compare multiple versions side-by-side."
    )
    parser.add_argument("db", help="Path to SQLite database")
    parser.add_argument("--ts", type=int, help="Filter by specific timestamp")
    parser.add_argument(
        "--metric",
        default="runtime",
        choices=[
            "runtime",
            "snr",
            "psnr",
            "rmse",
            "max_abs",
            "max_rel",
            "mean_abs",
            "mismatches",
            "gpumem",
        ],
        help="Metric to compare",
    )
    parser.add_argument("--atol", type=float, default=1e-12)
    parser.add_argument("--rtol", type=float, default=1e-12)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    # 1. Discovery
    if args.metric == "runtime":
        query_configs = "SELECT DISTINCT lowerprec, substeps FROM timings"
    elif args.metric == "gpumem":
        query_configs = "SELECT DISTINCT lowerprec, substeps FROM gpu_memory"
    else:
        query_configs = "SELECT DISTINCT lowerprec, substeps FROM fields"

    configs = conn.execute(query_configs).fetchall()
    if not configs:
        print("No data found in database.")
        return

    all_data = []

    # 2. Data Collection
    if args.metric == "runtime":
        query = "SELECT lowerprec, substeps, tag, repetition, time_us, timestamp FROM timings"
        params = []
        if args.ts is not None and args.ts > 0:
            query += " WHERE timestamp = ?"
            params.append(args.ts)

        rows = conn.execute(query, params).fetchall()
        if rows:
            raw_df = pl.DataFrame(
                rows,
                schema=[
                    "lowerprec",
                    "substeps",
                    "tag",
                    "repetition",
                    "time_us",
                    "timestamp",
                ],
                orient="row",
            )
            for (lp, ss, tag, ts_val), group in raw_df.group_by(
                ["lowerprec", "substeps", "tag", "timestamp"]
            ):
                if ss > 1:
                    # For substeps > 1, main_gpu.cu only runs 1 repetition (index 0)
                    # and we want the mean of the substeps.
                    m = group["time_us"].mean()
                    s = group["time_us"].std() if group.height > 1 else 0.0
                else:
                    if group.height < 2:
                        continue
                    first_val = group.filter(pl.col("repetition") == 0)["time_us"][0]
                    rest = group.filter(pl.col("repetition") > 0)["time_us"]
                    m = rest.mean()
                    s = rest.std() if rest.len() > 1 else 0.0

                    assert first_val > 2 * m, (
                        f"First entry ({first_val:.2f} us) not > 2x mean of rest ({m:.2f} us) "
                        f"for {tag} ({lp}_ss{ss}) at TS{ts_val}"
                    )

                all_data.append(
                    {
                        "ts": ts_val,
                        "item": tag,
                        "version": get_run_label(lp, ss),
                        "value": f"{m:.2f} +/- {s:.2f}",
                    }
                )
    elif args.metric == "gpumem":
        query = "SELECT lowerprec, substeps, timestamp, bytes_transferred, bytes_allocated, bytes_real, bytes_residency_increase FROM gpu_memory"
        params = []
        if args.ts is not None and args.ts > 0:
            query += " WHERE timestamp = ?"
            params.append(args.ts)

        rows = conn.execute(query, params).fetchall()
        for lp, ss, ts_val, bt, ba, br, bri in rows:
            label = get_run_label(lp, ss)
            fmt = lambda b: f"{b / 1024 / 1024:.2f} MB"
            all_data.append(
                {
                    "ts": ts_val,
                    "item": "1. Transferred",
                    "version": label,
                    "value": fmt(bt),
                }
            )
            all_data.append(
                {
                    "ts": ts_val,
                    "item": "2. Allocated (Tracked)",
                    "version": label,
                    "value": fmt(ba),
                }
            )
            all_data.append(
                {
                    "ts": ts_val,
                    "item": "3. Real (Driver)",
                    "version": label,
                    "value": fmt(br),
                }
            )
            all_data.append(
                {
                    "ts": ts_val,
                    "item": "4. Residency Increase",
                    "version": label,
                    "value": fmt(bri),
                }
            )
    else:
        # 2a. Discovery of Golden Baseline (fp64_ss1's want_blob)
        golden_baseline = {}
        golden_query = "SELECT timestamp, field_name, want_blob FROM fields WHERE lowerprec = 'fp64' AND substeps = 1"
        params = []
        if args.ts is not None and args.ts > 0:
            golden_query += " AND timestamp = ?"
            params.append(args.ts)

        for r_ts, r_field, r_blob in conn.execute(golden_query, params).fetchall():
            golden_baseline[(r_ts, r_field)] = decompress(r_blob)

        # 2b. Collect all rows for parallel processing
        all_rows = []
        for lp, ss in configs:
            label = get_run_label(lp, ss)
            # Fetch extra metadata to perform a proper ambiguity check before parallelizing
            query = "SELECT timestamp, field_name, got_blob, want_blob, nproma, istep, lvn_only, ldeepatmo, lowerprec, substeps FROM fields WHERE lowerprec = ? AND substeps = ?"
            params = [lp, ss]
            if args.ts is not None and args.ts > 0:
                query += " AND timestamp = ?"
                params.append(args.ts)

            cursor = conn.execute(query, params).fetchall()

            # Ambiguity check: Ensure unique config per (field, ts) for this version string
            configs_found = {}
            for row in cursor:
                r_ts, r_field, _, _, r_np, r_is, r_lvn, r_lda, r_lp, r_ss = row
                key = (r_ts, r_field)
                full_config = (r_np, r_is, r_lvn, r_lda, r_lp, r_ss)
                if key not in configs_found:
                    configs_found[key] = set()
                configs_found[key].add(full_config)

            for key, cfgs in configs_found.items():
                if len(cfgs) > 1:
                    raise ValueError(
                        f"Ambiguity in {label} for {key}: multiple setups found: {cfgs}. "
                        "The database contains conflicting runs for the same version string."
                    )

            # Pack rows for parallel processing (timestamp, field_name, got_blob, want_blob, lp, ss)
            all_rows.extend([(r[0], r[1], r[2], r[3], lp, ss) for r in cursor])

        # 2c. Parallel Processing
        worker = partial(
            process_row,
            metric=args.metric,
            atol=args.atol,
            rtol=args.rtol,
        )

        with ProcessPoolExecutor(
            initializer=init_worker, initargs=(golden_baseline,)
        ) as executor:
            chunk_results = list(executor.map(worker, all_rows))
            for res_list in chunk_results:
                all_data.extend(res_list)

    # 3. Presentation
    if not all_data:
        print("No matching data found.")
        return
    df = pl.DataFrame(all_data, orient="row")
    pivot_df = df.pivot(index=["ts", "item"], on="version", values="value").sort(
        ["ts", "item"]
    )
    if args.metric == "runtime":
        pivot_df = pivot_df.drop("item")

    # Sensible column ordering: Group by substeps first, then precision (fp16 < fp32 < fp64)
    def col_key(c):
        prec_map = {"fp16": 1, "fp32": 2, "fp64": 3}
        parts = c.split("_ss")
        ss = int(parts[1]) if len(parts) > 1 else 0
        prec = prec_map.get(parts[0], 99)
        return (ss, prec)

    data_cols = [c for c in pivot_df.columns if c not in ["ts", "item"]]
    sorted_data_cols = sorted(data_cols, key=col_key)
    index_cols = [c for c in ["ts", "item"] if c in pivot_df.columns]
    pivot_df = pivot_df.select(index_cols + sorted_data_cols)

    print(f"\nSHOWDOWN: Metric = {args.metric}")
    if args.metric == "runtime":
        print("Values shown as: Mean +/- StdDev (us)")
    if args.ts:
        print(f"Timestamp: {args.ts}")
    print("=" * 100)

    # Polars config for wide tables
    with pl.Config(
        tbl_rows=200,
        tbl_cols=-1,
        tbl_width_chars=10000,
        float_precision=6,
    ):
        table_str = str(pivot_df)

    import sys
    import subprocess

    if sys.stdout.isatty():
        # Pipe to less -S for horizontal scrolling in interactive terminal
        process = subprocess.Popen(["less", "-S"], stdin=subprocess.PIPE, text=True)
        process.communicate(input=table_str)
    else:
        print(table_str)

    print("=" * 100)


if __name__ == "__main__":
    main()
