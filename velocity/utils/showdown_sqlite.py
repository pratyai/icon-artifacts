import argparse
import sqlite3
import zstandard as zstd
import numpy as np
import polars as pl
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
            "max_abs": -1.0,
            "max_rel": -1.0,
            "mae": -1.0,
            "rmse": -1.0,
            "snr_db": -1.0,
            "mismatches": abs(got.size - want.size),
            "count": got.size,
            "has_nan": False,
            "max_norm_want": -1.0,
            "l2_norm_want": -1.0,
            "error": f"Size mismatch: {got.size} vs {want.size}",
        }

    # Convert to float64 for stable comparison and to avoid overflow in norms
    got_64 = got.astype(np.float64)
    want_64 = want.astype(np.float64)

    abs_diff = np.abs(got_64 - want_64)
    # Use fmax to ignore NaNs in the scaling factor and ensure it is at least abs_tol
    scale = np.fmax(np.fmax(np.abs(got_64), np.abs(want_64)), abs_tol)
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
        "mae": np.mean(abs_diff) if abs_diff.size > 0 else 0.0,
        "rmse": norm_diff / np.sqrt(got.size) if got.size > 0 else 0.0,
        "snr_db": snr_db,
        "mismatches": int(mismatches),
        "count": got.size,
        "has_nan": not (np.all(np.isfinite(got_64)) and np.all(np.isfinite(want_64))),
        "max_norm_want": np.max(np.abs(want_64)) if want_64.size > 0 else 0.0,
        "l2_norm_want": norm_want,
    }


# Metadata constants
TEXT_FIELDS = {"global_data", "p_diag", "p_metrics", "p_prog", "p_int", "p_patch"}
KNOWN_METADATA = {"assoc", "rank", "size", "lbound", "entries"}

D_CTX = zstd.ZstdDecompressor()


def decompress(blob: bytes) -> bytes:
    return D_CTX.decompress(blob)


def parse_serialized_text(text: str) -> Dict[str, np.ndarray]:
    results, current_var, chunks = {}, "root", []

    def flush(var, c_list):
        if not c_list:
            return
        val = np.fromstring(" ".join(c_list), sep=" ")
        results[var] = np.concatenate([results[var], val]) if var in results else val

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# "):
            flush(current_var, chunks)
            chunks = []
            tag = line.lstrip("# ").strip().split()[0]
            if tag not in KNOWN_METADATA:
                current_var = tag
        else:
            chunks.append(line)
    flush(current_var, chunks)
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
        choices=["runtime", "snr", "rmse", "max_abs", "max_rel", "mae", "mismatches"],
        help="Metric to compare",
    )
    parser.add_argument("--atol", type=float, default=1e-12)
    parser.add_argument("--rtol", type=float, default=1e-12)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    # 1. Discovery
    if args.metric == "runtime":
        query_configs = "SELECT DISTINCT lowerprec, substeps FROM timings"
    else:
        query_configs = "SELECT DISTINCT lowerprec, substeps FROM fields"

    configs = conn.execute(query_configs).fetchall()
    if not configs:
        print("No data found in database.")
        return

    all_data = []

    # 2. Data Collection
    if args.metric == "runtime":
        query = "SELECT lowerprec, substeps, tag, AVG(time_us) FROM timings"
        params = []
        if args.ts:
            query += " WHERE timestamp = ?"
            params.append(args.ts)
        query += " GROUP BY lowerprec, substeps, tag"

        rows = conn.execute(query, params).fetchall()
        for lp, ss, tag, val in rows:
            all_data.append(
                {"item": tag, "version": get_run_label(lp, ss), "value": val}
            )
    else:
        # Numerical metrics require processing blobs
        for lp, ss in configs:
            label = get_run_label(lp, ss)
            query = "SELECT timestamp, field_name, got_blob, want_blob, istep, lvn_only, nproma, ldeepatmo FROM fields WHERE lowerprec = ? AND substeps = ?"
            params = [lp, ss]
            if args.ts:
                query += " AND timestamp = ?"
                params.append(args.ts)

            cursor = conn.execute(query, params).fetchall()

            # Ambiguity check: Ensure unique config per (field, ts) for this version
            configs_found = {}
            for row in cursor:
                r_ts, r_field, _, _, r_istep, r_lvn, r_nproma, r_ldeep = row
                key = (r_ts, r_field)
                if key not in configs_found:
                    configs_found[key] = set()
                configs_found[key].add((r_istep, r_lvn, r_nproma, r_ldeep))

            for key, cfgs in configs_found.items():
                if len(cfgs) > 1:
                    raise ValueError(
                        f"Ambiguity in {label} for {key}: multiple setups {cfgs}. "
                        "The database contains conflicting runs for the same version string."
                    )

            for row in cursor:
                ts, field_name, got_z, want_z = row[0], row[1], row[2], row[3]
                got_raw, want_raw = decompress(got_z), decompress(want_z)
                # If multiple TS are present, include TS in the row name to avoid pivot collisions
                item_prefix = f"TS{ts} | " if not args.ts else ""

                if field_name in TEXT_FIELDS:
                    got_dict = parse_serialized_text(got_raw.decode("utf-8"))
                    want_dict = parse_serialized_text(want_raw.decode("utf-8"))
                    for var in got_dict:
                        if var in want_dict:
                            stats = compare_data(
                                got_dict[var],
                                want_dict[var],
                                abs_tol=args.atol,
                                rel_tol=args.rtol,
                            )
                            all_data.append(
                                {
                                    "item": f"{item_prefix}{field_name} % {var}",
                                    "version": label,
                                    "value": stats[
                                        args.metric
                                        if args.metric != "snr"
                                        else "snr_db"
                                    ],
                                }
                            )
                else:
                    dtype = (
                        np.float16
                        if "half" in lp.lower() or "fp16" in lp.lower()
                        else np.float32
                        if "float" in lp.lower() or "fp32" in lp.lower()
                        else np.float64
                    )
                    stats = compare_data(
                        np.frombuffer(got_raw, dtype=dtype),
                        np.frombuffer(want_raw, dtype=dtype),
                        abs_tol=args.atol,
                        rel_tol=args.rtol,
                    )
                    all_data.append(
                        {
                            "item": f"{item_prefix}{field_name}",
                            "version": label,
                            "value": stats[
                                args.metric if args.metric != "snr" else "snr_db"
                            ],
                        }
                    )

    if not all_data:
        print("No matching data found.")
        return

    # 3. Presentation
    df = pl.DataFrame(all_data, orient="row")
    pivot_df = df.pivot(index="item", on="version", values="value").sort("item")

    print(f"\nSHOWDOWN: Metric = {args.metric}")
    if args.ts:
        print(f"Timestamp: {args.ts}")
    print("=" * 100)
    with pl.Config(tbl_rows=100, tbl_width_chars=200, float_precision=4):
        print(pivot_df)
    print("=" * 100)


if __name__ == "__main__":
    main()
