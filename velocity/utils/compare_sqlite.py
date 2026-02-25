import argparse
import sqlite3
import zstandard as zstd
import numpy as np
import polars as pl
from typing import Dict, List, Any


def compare_data(
    got: np.ndarray,
    want: np.ndarray,
    abs_tol: float = np.finfo(np.float64).eps,
    rel_tol: float = np.finfo(np.float64).eps,
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
    with np.errstate(divide="ignore", invalid="ignore"):
        rel_diff = abs_diff / scale

    # Mismatch count logic matching math.isclose
    is_close = abs_diff <= np.maximum(
        rel_tol * np.maximum(np.abs(got), np.abs(want)), abs_tol
    )
    mismatches = np.count_nonzero(~is_close)

    # Use linalg.norm for stability and handle potential overflows in sum of squares
    norm_diff = np.linalg.norm(abs_diff)
    norm_want = np.linalg.norm(want)

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
        "has_nan": not (np.all(np.isfinite(got)) and np.all(np.isfinite(want))),
        "max_norm_want": np.max(np.abs(want)) if want.size > 0 else 0.0,
        "l2_norm_want": norm_want,
    }


def decompress(blob: bytes) -> bytes:
    return zstd.ZstdDecompressor().decompress(blob)


def parse_serialized_text(text: str) -> Dict[str, np.ndarray]:
    results = {}
    current_var = "root"
    KNOWN_METADATA = {"assoc", "rank", "size", "lbound", "entries"}
    chunks = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# "):
            if chunks:
                val = np.fromstring(" ".join(chunks), sep=" ")
                results[current_var] = (
                    np.concatenate([results[current_var], val])
                    if current_var in results
                    else val
                )
                chunks = []
            tag = line.lstrip("# ").strip().split()[0]
            if tag and tag not in KNOWN_METADATA:
                current_var = tag
        else:
            chunks.append(line)

    if chunks:
        val = np.fromstring(" ".join(chunks), sep=" ")
        results[current_var] = (
            np.concatenate([results[current_var], val])
            if current_var in results
            else val
        )
    return results


def get_blobs(conn, lp, ts, subs, col="got_blob"):
    query = f"SELECT timestamp, field_name, {col}, lowerprec, istep, lvn_only, nproma, ldeepatmo FROM fields WHERE lowerprec = ? AND timestamp = ? AND substeps = ?"
    rows = conn.execute(query, [lp, ts, subs]).fetchall()

    res = {}
    configs_found = {}
    for row in rows:
        r_ts, r_field, r_blob, r_lp, r_istep, r_lvn, r_nproma, r_ldeep = row
        key = (r_ts, r_field)
        if key not in configs_found:
            configs_found[key] = set()
        configs_found[key].add((r_istep, r_lvn, r_nproma, r_ldeep))
        res[key] = (r_blob, r_lp)

    for key, cfgs in configs_found.items():
        if len(cfgs) > 1:
            raise ValueError(
                f"Ambiguous configuration for {key} (lp={lp}, subs={subs}): multiple setups found {cfgs}."
            )
    return res


def main():
    parser = argparse.ArgumentParser(
        description="Compare GPU results against reference in SQLite."
    )
    parser.add_argument("db", help="Path to SQLite database")
    parser.add_argument("--ts", type=int, help="Timestep to compare")
    parser.add_argument(
        "-q", action="store_true", help="Query available data combinations"
    )
    parser.add_argument(
        "--lp", default="fp64", help="lowerprec for Test side (default: fp64)"
    )
    parser.add_argument(
        "--subs", type=int, default=1, help="subs for Test side (default: 1)"
    )
    parser.add_argument(
        "--w-lp", default="fp64", help="lowerprec for Reference side (default: fp64)"
    )
    parser.add_argument(
        "--w-subs", type=int, default=1, help="subs for Reference side (default: 1)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    def map_lp(lp_str):
        mapping = {"fp64": "double", "fp32": "float"}
        return mapping.get(lp_str.lower(), lp_str)

    lp_test_mapped, lp_ref_mapped = map_lp(args.lp), map_lp(args.w_lp)

    try:
        conn = sqlite3.connect(args.db)
    except Exception as e:
        print(f"Error connecting to DB: {e}")
        return

    if args.q:
        combos = conn.execute(
            "SELECT DISTINCT timestamp, substeps, lowerprec FROM fields ORDER BY 1,2,3"
        ).fetchall()
        print("\nAvailable combinations:")
        print(pl.DataFrame(combos, schema=["timestamp", "substeps", "lowerprec"]))
        return

    if args.ts is None:
        parser.error("--ts is required")

    db_lps = [
        r[0] for r in conn.execute("SELECT DISTINCT lowerprec FROM fields").fetchall()
    ]

    def find_best_lp(target, available):
        t_can = map_lp(target)
        for a in available:
            if map_lp(a) == t_can:
                return a
        return target

    q_lp_test, q_lp_ref = (
        find_best_lp(lp_test_mapped, db_lps),
        find_best_lp(lp_ref_mapped, db_lps),
    )

    print(f"\nComparing at TS {args.ts}")
    print(f"  Test Side (Got): lp={q_lp_test} (from {args.lp}), subs={args.subs}")
    print(f"  Ref Side (Want): lp={q_lp_ref} (from {args.w_lp}), subs={args.w_subs}\n")

    try:
        test_data = get_blobs(conn, q_lp_test, args.ts, args.subs, "got_blob")
        ref_data = get_blobs(conn, q_lp_ref, args.ts, args.w_subs, "want_blob")
    except Exception as e:
        print(f"Error: {e}")
        return

    TEXT_FIELDS = {
        "global_data",
        "p_diag",
        "p_metrics",
        "p_prog",
        "p_int",
        "p_patch",
        "z_kin_hor_e",
        "z_vt_ie",
        "z_w_concorr_me",
    }
    all_stats = []

    for key in sorted(test_data.keys()):
        if key not in ref_data:
            continue
        ts, field_name = key
        got_z, lp_test = test_data[key]
        want_z, lp_ref = ref_data[key]

        try:
            got_raw, want_raw = decompress(got_z), decompress(want_z)
        except Exception as e:
            print(f"Decompression error for {field_name}: {e}")
            continue

        got_dict = parse_serialized_text(got_raw.decode("utf-8"))
        want_dict = parse_serialized_text(want_raw.decode("utf-8"))

        if field_name in TEXT_FIELDS:
            for var in got_dict:
                if var in want_dict:
                    stats = compare_data(got_dict[var], want_dict[var])
                    all_stats.append(
                        {
                            "timestamp": ts,
                            "field_name": field_name,
                            "sub_field": var,
                            "status": "OK" if stats["ok"] else "DIFF",
                            **{
                                k: stats[k]
                                for k in [
                                    "max_abs",
                                    "max_rel",
                                    "mae",
                                    "rmse",
                                    "snr_db",
                                    "mismatches",
                                    "count",
                                ]
                            },
                        }
                    )
        else:
            # Simple arrays store their entries under the default "root" tag in parse_serialized_text
            stats = compare_data(
                got_dict["root"],
                want_dict["root"],
                abs_tol=args.atol,
                rel_tol=args.rtol,
            )
            all_stats.append(
                {
                    "timestamp": ts,
                    "field_name": field_name,
                    "sub_field": "-",
                    "status": "OK" if stats["ok"] else "DIFF",
                    **{
                        k: stats[k]
                        for k in [
                            "max_abs",
                            "max_rel",
                            "mae",
                            "rmse",
                            "snr_db",
                            "mismatches",
                            "count",
                        ]
                    },
                }
            )

    if not all_stats:
        print("No matching data found.")
        return

    df = pl.DataFrame(all_stats, orient="row").rename(
        {"count": "total_elements", "timestamp": "timestep"}
    )

    # Create a merged field column for display consistency
    df = df.with_columns(
        field=pl.when(pl.col("sub_field") == "-")
        .then(pl.col("field_name"))
        .otherwise(pl.col("field_name") + " % " + pl.col("sub_field"))
    )

    with pl.Config(tbl_rows=100, tbl_width_chars=200, float_precision=4):
        print(
            df.select(
                [
                    "timestep",
                    "field",
                    "status",
                    "max_abs",
                    "max_rel",
                    "rmse",
                    "snr_db",
                    "mismatches",
                    "total_elements",
                ]
            )
        )

    csv_out = f"{args.db.replace('.db', '')}_ts{args.ts}_comparison.csv"
    df.write_csv(csv_out)
    print(f"\nFull results saved to: {csv_out}")


if __name__ == "__main__":
    main()
