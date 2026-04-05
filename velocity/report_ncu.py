#!/usr/bin/env python3
"""Usage: ./report_ncu.py [--detail] [PATTERN [PATTERN ...]]

Reads ncu_summary.csv and ncu_aggregate.csv and prints a report.
  --detail       Also show per-kernel detail (default: aggregate + dominant only)
  PATTERN        Only show files matching ALL patterns (e.g. 'fp16' 'r02b03')

Examples:
  ./report_ncu.py                      # everything, aggregate + dominant
  ./report_ncu.py fp16                 # only fp16 files
  ./report_ncu.py r02b03 fp64          # only r02b03 AND fp64
  ./report_ncu.py --detail r02b03      # r02b03 with per-kernel detail
"""
import csv, sys, os

def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def fmt(val):
    v = val.strip().replace(",", "")
    try:
        fv = float(v)
        if abs(fv) >= 1e6:
            return f"{fv:,.0f}"
        elif abs(fv) >= 1:
            return f"{fv:,.2f}"
        else:
            return f"{fv:.4g}"
    except ValueError:
        return val

def matches(filename, patterns):
    return all(p in filename for p in patterns)

def main():
    detail_mode = "--detail" in sys.argv
    patterns = [a for a in sys.argv[1:] if a != "--detail"]

    summary = load_csv("ncu_summary.csv")
    aggregate = load_csv("ncu_aggregate.csv")

    if not summary and not aggregate:
        print("No ncu_summary.csv or ncu_aggregate.csv found. Run extract_ncu.py first.")
        sys.exit(1)

    # --- Aggregate + Dominant ---
    if aggregate:
        files = []
        by_file = {}
        for row in aggregate:
            f = row["file"]
            if patterns and not matches(f, patterns):
                continue
            if f not in by_file:
                by_file[f] = []
                files.append(f)
            by_file[f].append(row)

        for f in files:
            rows = by_file[f]
            agg_rows = [r for r in rows if r["kernel"] == "AGGREGATE"]
            dom_rows = [r for r in rows if r["kernel"].startswith("DOMINANT:")]

            print(f"\n{'='*60}")
            print(f"  {f}")
            print(f"{'='*60}")

            if agg_rows:
                cold = {r["metric"]: r["value"] for r in agg_rows if r.get("warmth") == "cold"}
                warm = {r["metric"]: r["value"] for r in agg_rows if r.get("warmth") == "warm"}
                metrics = list(dict.fromkeys(r["metric"] for r in agg_rows))
                print(f"\n  [AGGREGATE]")
                print(f"    {'Metric':<25s}      {'cold':>15s}      {'warm':>15s}")
                print(f"    {'-'*25}      {'-'*15}      {'-'*15}")
                for m in metrics:
                    c = fmt(cold.get(m, "n/a"))
                    w = fmt(warm.get(m, "n/a"))
                    print(f"    {m:<25s}      {c:>15s}      {w:>15s}")

            if dom_rows:
                cold_rows = [r for r in dom_rows if r.get("warmth") == "cold"]
                warm_rows = [r for r in dom_rows if r.get("warmth") == "warm"]
                cold_name = cold_rows[0]["kernel"] if cold_rows else ""
                warm_name = warm_rows[0]["kernel"] if warm_rows else ""
                cold = {r["metric"]: (r["value"], r.get("unit", "")) for r in cold_rows}
                warm = {r["metric"]: (r["value"], r.get("unit", "")) for r in warm_rows}
                metrics = list(dict.fromkeys(r["metric"] for r in dom_rows))
                # Compute duration % from aggregate totals
                def _dur_pct(dom_dict, agg_dict):
                    dv, du = dom_dict.get("Duration", ("0", "us"))
                    try:
                        d = float(dv.replace(",", ""))
                    except ValueError:
                        return ""
                    try:
                        total = float(agg_dict.get("Total duration [us]", "0").replace(",", ""))
                    except ValueError:
                        return ""
                    # Duration unit → us
                    u = du.strip().lower()
                    if "ms" in u: d *= 1e3
                    elif "ns" in u: d *= 1e-3
                    elif u == "s": d *= 1e6
                    if total > 0:
                        return f"  ({d:.1f} us, {d/total*100:.1f}%)"
                    return f"  ({d:.1f} us)"
                cold_agg = {r["metric"]: r["value"] for r in agg_rows if r.get("warmth") == "cold"}
                warm_agg = {r["metric"]: r["value"] for r in agg_rows if r.get("warmth") == "warm"}
                cold_info = _dur_pct(cold, cold_agg)
                warm_info = _dur_pct(warm, warm_agg)
                print(f"\n  [DOMINANT cold: {cold_name}{cold_info}]")
                if cold_name == warm_name:
                    print(f"  [DOMINANT warm: (same){warm_info}]")
                else:
                    print(f"  [DOMINANT warm: {warm_name}{warm_info}]")
                print(f"    {'Metric':<25s}      {'cold':>15s}      {'warm':>15s}")
                print(f"    {'-'*25}      {'-'*15}      {'-'*15}")
                for m in metrics:
                    cv, cu = cold.get(m, ("n/a", ""))
                    wv, wu = warm.get(m, ("n/a", ""))
                    unit = cu or wu
                    cs = f"{cv} {unit}".strip() if cv != "n/a" else "n/a"
                    ws = f"{wv} {unit}".strip() if wv != "n/a" else "n/a"
                    print(f"    {m:<25s}      {cs:>15s}      {ws:>15s}")

    # --- Per-kernel detail ---
    if detail_mode and summary:
        by_file_kernel_rep = {}
        files = []
        for row in summary:
            f = row["file"]
            if patterns and not matches(f, patterns):
                continue
            k = row["kernel"]
            rep = row.get("rep", "0")
            key = (f, k, rep)
            if f not in files:
                files.append(f)
            if key not in by_file_kernel_rep:
                by_file_kernel_rep[key] = []
            by_file_kernel_rep[key].append(row)

        for f in files:
            print(f"\n{'='*60}")
            print(f"  {f}  [PER-KERNEL DETAIL]")
            print(f"{'='*60}")
            kr_pairs = []
            for (ff, k, rep) in by_file_kernel_rep:
                if ff == f and (k, rep) not in kr_pairs:
                    kr_pairs.append((k, rep))
            for k, rep in kr_pairs:
                print(f"\n  [{k}] rep={rep}")
                for r in by_file_kernel_rep[(f, k, rep)]:
                    v = r["value"]
                    u = r.get("unit", "")
                    print(f"    {r['metric']:<25s} {v:>15s} {u}")

    print()

if __name__ == "__main__":
    main()
