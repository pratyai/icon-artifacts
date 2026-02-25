import argparse
from concurrent.futures import ProcessPoolExecutor
from itertools import zip_longest
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Tuple
from functools import partial
import numpy as np
import math
from pathlib import Path
import polars as pl

sys.stdout.reconfigure(line_buffering=True)


DEFAULT_WORKERS = int(
    os.environ.get(
        "SLURM_CPUS_PER_TASK", os.environ.get("SLURM_CPUS_ON_NODE", os.cpu_count() or 1)
    )
)


def discover_timesteps(root: Path) -> List[int]:
    ts_set: set[int] = set()
    pat = re.compile(r"_(\d+)\.got$")
    for p in root.glob("*.got"):
        m = pat.search(p.name)
        if m:
            ts_set.add(int(m.group(1)))
    for p in root.glob("*.want"):
        m = pat.search(p.name)
        if m:
            ts_set.add(int(m.group(1)))
    return sorted(ts_set)


def find_comparable_files_at_timestep(
    timestep: int, root: Path
) -> List[Tuple[Path, Path]]:
    pairs: list[tuple[str, str]] = []

    for want in root.glob("*.want"):
        if not want.name.endswith(f"_{timestep}.want"):
            continue
        got = want.with_suffix(".got")
        if not got.is_file():
            print(f"⚠️  Skipping {want}: matching {got} not found")
            continue
        pairs.append((got, want))

    return pairs


def _stream_lines(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            ls = line.rstrip("\n\r")
            if not ls.strip():
                continue
            yield ls


KNOWN_METADATA = {
    "assoc",
    "rank",
    "size",
    "lbound",
    "entries",
}


def compare_pair(
    got: Path,
    want: Path,
    abs_tol: float = np.finfo(np.float64).eps,
    rel_tol: float = np.finfo(np.float64).eps,
    verbose: bool = True,
) -> Tuple[Optional[str], Dict[str, Dict[str, float]]]:
    if verbose:
        print(f"Comparing {got} vs. {want}")

    per_var: Dict[str, Dict[str, float]] = {}
    current_var: str = got.stem
    got_accum: List[float] = []
    want_accum: List[float] = []

    def flush_var(var_name: str, g_list: List[float], w_list: List[float]):
        if not g_list:
            return
        g = np.array(g_list)
        w = np.array(w_list)

        abs_diff = np.abs(g - w)
        scale = np.maximum(np.maximum(np.abs(g), np.abs(w)), abs_tol)
        rel_diff = abs_diff / scale

        # Mismatch count logic matching math.isclose
        is_close = abs_diff <= np.fmax(rel_tol * np.fmax(np.abs(g), np.abs(w)), abs_tol)
        mismatches = np.count_nonzero(~is_close)

        norm_diff = np.linalg.norm(abs_diff)
        norm_w = np.linalg.norm(w)

        if norm_diff == 0:
            snr_db = float("inf")
        elif norm_w == 0:
            snr_db = -float("inf")
        else:
            snr_db = 20 * np.log10(norm_w / norm_diff)

        per_var[var_name] = {
            "ok": mismatches == 0,
            "max_abs": np.max(abs_diff) if abs_diff.size > 0 else 0.0,
            "max_rel": np.max(rel_diff) if rel_diff.size > 0 else 0.0,
            "mae": np.mean(abs_diff) if abs_diff.size > 0 else 0.0,
            "rmse": norm_diff / np.sqrt(g.size) if g.size > 0 else 0.0,
            "snr_db": snr_db,
            "mismatches": int(mismatches),
            "count": g.size,
            "has_nan": not (np.all(np.isfinite(g)) and np.all(np.isfinite(w))),
            "max_norm_want": np.max(np.abs(w)) if w.size > 0 else 0.0,
            "l2_norm_want": norm_w,
        }

    for got_line, want_line in zip_longest(_stream_lines(got), _stream_lines(want)):
        if got_line is None or want_line is None:
            msg = f"Different number of lines ❌"
            if verbose:
                print(msg)
            return msg, per_var

        if got_line.startswith("# ") or want_line.startswith("# "):
            if got_line != want_line:
                msg = f"Different text ({got_line} vs. {want_line}) ❌"
                if verbose:
                    print(msg)
                return msg, per_var

            # Flush current variable before starting a new one
            flush_var(current_var, got_accum, want_accum)
            got_accum, want_accum = [], []

            tag = got_line.lstrip("# ").strip().split()[0]
            if tag and tag not in KNOWN_METADATA:
                current_var = tag
                if verbose:
                    print(f"Checking: {current_var}")
            continue

        try:
            got_accum.append(float(got_line))
            want_accum.append(float(want_line))
        except ValueError:
            msg = f"Non-numeric data for `{current_var}` ({got_line} & {want_line}) ❌"
            if verbose:
                print(msg)
            return msg, per_var

    # Final flush
    flush_var(current_var, got_accum, want_accum)

    ok = all(v["ok"] for v in per_var.values())
    msg = None if ok else "Numerical differences found ❌"
    if msg and verbose:
        print(msg)
    return msg, per_var


POLARS_SCHEMA = {
    "timestep": pl.Int64,
    "got_file": pl.Utf8,
    "want_file": pl.Utf8,
    "variable": pl.Utf8,
    "status": pl.Utf8,
    "max_abs": pl.Float64,
    "max_rel": pl.Float64,
    "mae": pl.Float64,
    "rmse": pl.Float64,
    "snr_db": pl.Float64,
    "mismatches": pl.Int64,
    "total_elements": pl.Int64,
    "max_norm_want": pl.Float64,
    "l2_norm_want": pl.Float64,
}


def make_comparison_for_timestep(ts: int, root: Path) -> Tuple[int, pl.DataFrame]:
    T = pl.DataFrame(schema=POLARS_SCHEMA)
    fpairs = find_comparable_files_at_timestep(ts, root)
    for got, want in fpairs:
        err, per_var = compare_pair(got, want, abs_tol=atol, rel_tol=rtol)
        if not err:
            print(f"{got.name} vs. {want.name} : No numerical differences found ✅")
        else:
            print(f"{got.name} vs. {want.name} : {err}")
        for var, st in per_var.items():
            status = "OK" if st["ok"] else "DIFF"
            if status == "DIFF":
                nan_msg = " (NaN/Inf found! ☢️)" if st["has_nan"] else ""
                print(
                    f"  {var}: {status}{nan_msg} | mismatches={st['mismatches']}/{st['count']} | rmse={st['rmse']} | snr={st['snr_db']:.2f}dB"
                )
            T.extend(
                pl.DataFrame(
                    {
                        "timestep": ts,
                        "got_file": got.name,
                        "want_file": want.name,
                        "variable": var,
                        "status": status,
                        "max_abs": st["max_abs"],
                        "max_rel": st["max_rel"],
                        "mae": st["mae"],
                        "rmse": st["rmse"],
                        "snr_db": st["snr_db"],
                        "mismatches": st["mismatches"],
                        "total_elements": st["count"],
                        "max_norm_want": st["max_norm_want"],
                        "l2_norm_want": st["l2_norm_want"],
                    },
                    schema=POLARS_SCHEMA,
                )
            )
        sys.stdout.flush()
    return ts, T


if __name__ == "__main__":
    argp = argparse.ArgumentParser(
        description="Run compare_got_and_want with a timestep."
    )
    argp.add_argument(
        "-r",
        "--root",
        type=str,
        default=".",
        help="Root directory for the got and want files.",
    )
    argp.add_argument(
        "timesteps",
        type=str,
        nargs="?",
        default="",
        help="Comma-separated list of timesteps. If omitted, all timesteps are processed.",
    )
    args = argp.parse_args()

    root = Path(args.root)
    print(f"Looking at: {root}")

    timesteps = [ts.strip() for ts in args.timesteps.split(",") if ts.strip()]
    assert all(ts.isdigit() for ts in timesteps)
    timesteps = [int(ts) for ts in timesteps]
    if not timesteps:
        timesteps = discover_timesteps(root)
    print(f"Comparing for timesteps: {timesteps}")
    print(f"Will use {DEFAULT_WORKERS} workers.")

    all_results = []
    with ProcessPoolExecutor(max_workers=DEFAULT_WORKERS) as ex:
        for ts, T in ex.map(
            partial(make_comparison_for_timestep, root=root), timesteps
        ):
            csvpath = root.joinpath(Path(f"numeric_differences_ts={ts}.csv"))
            print(f"Saving to: {csvpath}")
            T = T.sort(["got_file", "variable"])
            T.write_csv(csvpath, float_precision=None)
            all_results.append(T)

    if all_results:
        master_T = pl.concat(all_results).sort(["timestep", "got_file", "variable"])
        print("\n" + "=" * 80)
        print("FINAL COMPARISON SUMMARY")
        print("=" * 80)

        # Merge file and variable into a single 'field' column for cleaner display
        summary_display = master_T.with_columns(
            field=(
                pl.col("got_file").str.replace(r"_\d+\.got$", "")
                + " % "
                + pl.col("variable")
            ),
        ).select(
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

        with pl.Config(tbl_rows=100, tbl_width_chars=200):
            if summary_display["timestep"].n_unique() == 1:
                print(summary_display.drop("timestep"))
            else:
                print(summary_display)
        print("=" * 80)
