#!/usr/bin/env python3
"""Usage: ./extract_ncu.py <file.ncu-rep> [<file.ncu-rep> ...]

Per-kernel metrics, aggregate sums, derived ratios, and dominant kernel — all in one.
"""
import csv, sys, subprocess, re, os
from collections import OrderedDict, defaultdict
from concurrent.futures import ProcessPoolExecutor

ALL_METRICS = [
    # Instructions
    ("Executed Instructions",                                "Exec insns"),
    ("smsp__inst_executed.sum",                              "Total insns"),
    ("smsp__inst_executed_op_global_ld.sum",                 "Global ld insns"),
    ("smsp__inst_executed_op_global_st.sum",                 "Global st insns"),
    ("smsp__inst_executed_op_shared_ld.sum",                 "Shared ld insns"),
    ("smsp__inst_executed_op_shared_st.sum",                 "Shared st insns"),
    ("smsp__inst_executed_op_integer.sum",                   "Int insns"),
    ("smsp__inst_executed_op_fp16.sum",                      "FP16 insns"),
    ("smsp__inst_executed_op_fp32.sum",                      "FP32 insns"),
    ("smsp__inst_executed_op_fp64.sum",                      "FP64 insns"),
    ("smsp__inst_executed_op_conversion.sum",                "Conv insns"),
    ("sm__sass_thread_inst_executed_op_memory_pred_on.sum",  "Mem insns (thr)"),
    # FLOPs
    ("smsp__sass_thread_inst_executed_op_hfma_pred_on.sum",  "HFMA"),
    ("smsp__sass_thread_inst_executed_op_hmul_pred_on.sum",  "HMUL"),
    ("smsp__sass_thread_inst_executed_op_hadd_pred_on.sum",  "HADD"),
    ("smsp__sass_thread_inst_executed_op_ffma_pred_on.sum",  "FFMA"),
    ("smsp__sass_thread_inst_executed_op_fmul_pred_on.sum",  "FMUL"),
    ("smsp__sass_thread_inst_executed_op_fadd_pred_on.sum",  "FADD"),
    ("smsp__sass_thread_inst_executed_op_dfma_pred_on.sum",  "DFMA"),
    ("smsp__sass_thread_inst_executed_op_dmul_pred_on.sum",  "DMUL"),
    ("smsp__sass_thread_inst_executed_op_dadd_pred_on.sum",  "DADD"),
    # Memory bytes
    ("dram__bytes.sum",                                      "DRAM total"),
    ("dram__bytes_read.sum",                                 "DRAM rd"),
    ("dram__bytes_write.sum",                                "DRAM wr"),
    ("lts__t_bytes_lookup_hit.sum",                          "L2 hit bytes"),
    ("lts__t_bytes_lookup_miss.sum",                         "L2 miss bytes"),
    ("lts__t_sectors_lookup_hit.sum",                        "L2 hit sectors"),
    ("lts__t_sectors_lookup_miss.sum",                       "L2 miss sectors"),
    ("l1tex__t_bytes_lookup_hit.sum",                        "L1 hit bytes"),
    ("l1tex__t_bytes_lookup_miss.sum",                       "L1 miss bytes"),
    ("lts__t_bytes_equiv_l1sectormiss_pipe_lsu_mem_global_op_ld.sum", "L2 global ld bytes"),
    ("lts__t_bytes_equiv_l1sectormiss_pipe_lsu_mem_global_op_st.sum", "L2 global st bytes"),
    # Throughput / utilization
    ("Compute (SM) Throughput",                              "Comp thru [%]"),
    ("Memory Throughput",                                    "Mem thru"),
    ("DRAM Throughput",                                      "DRAM thru [%]"),
    ("Duration",                                             "Duration"),
    ("L2 Hit Rate",                                          "L2 hit [%]"),
    ("L1/TEX Hit Rate",                                      "L1 hit [%]"),
    ("Mem Busy",                                             "Mem Busy [%]"),
    ("SM Busy",                                              "SM Busy [%]"),
    # Launch / occupancy
    ("Registers Per Thread",                                 "Regs/thr"),
    ("launch__shared_mem_per_block_static",                  "Shmem static"),
    ("launch__shared_mem_per_block_dynamic",                 "Shmem dynamic"),
    ("Achieved Occupancy",                                   "Occ [%]"),
    ("Theoretical Occupancy",                                "Theo Occ [%]"),
    ("Block Size",                                           "Block Size"),
    ("Grid Size",                                            "Grid Size"),
    ("Waves Per SM",                                         "Waves/SM"),
    # Warp / scheduler
    ("sm__warps_active.sum",                                 "Warps active"),
    ("sm__warps_active.avg.per_cycle_active",                "Warps/cyc active"),
    ("smsp__warps_launched.sum",                             "Warps launched"),
    ("Avg. Active Threads Per Warp",                         "Act Thr/Warp"),
    ("Warp Cycles Per Executed Instruction",                 "Warp Cyc/Inst"),
    ("Eligible Warps Per Scheduler",                         "Elig Warps/Sched"),
    ("No Eligible",                                          "No Eligible [%]"),
    # Coalescing
    ("smsp__sass_average_data_bytes_per_sector_mem_global_op_ld.pct", "Coal ld [%]"),
    ("smsp__sass_average_data_bytes_per_sector_mem_global_op_st.pct", "Coal st [%]"),
    ("l1tex__average_t_sectors_per_request_pipe_lsu_mem_global_op_ld.ratio", "Sec/req ld"),
    ("l1tex__average_t_sectors_per_request_pipe_lsu_mem_global_op_st.ratio", "Sec/req st"),
]

ALL_METRIC_NAMES = {m[0] for m in ALL_METRICS}

# Metrics that are raw counters and can be summed
SUMMABLE = {
    "Executed Instructions",
    "smsp__inst_executed.sum",
    "smsp__inst_executed_op_global_ld.sum",
    "smsp__inst_executed_op_global_st.sum",
    "smsp__inst_executed_op_shared_ld.sum",
    "smsp__inst_executed_op_shared_st.sum",
    "smsp__inst_executed_op_integer.sum",
    "smsp__inst_executed_op_fp16.sum",
    "smsp__inst_executed_op_fp32.sum",
    "smsp__inst_executed_op_fp64.sum",
    "smsp__inst_executed_op_conversion.sum",
    "sm__sass_thread_inst_executed_op_memory_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_hfma_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_hmul_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_hadd_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_ffma_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_fmul_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_fadd_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_dfma_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_dmul_pred_on.sum",
    "smsp__sass_thread_inst_executed_op_dadd_pred_on.sum",
    "dram__bytes.sum",
    "dram__bytes_read.sum",
    "dram__bytes_write.sum",
    "lts__t_bytes_lookup_hit.sum",
    "lts__t_bytes_lookup_miss.sum",
    "lts__t_sectors_lookup_hit.sum",
    "lts__t_sectors_lookup_miss.sum",
    "l1tex__t_bytes_lookup_hit.sum",
    "l1tex__t_bytes_lookup_miss.sum",
    "lts__t_bytes_equiv_l1sectormiss_pipe_lsu_mem_global_op_ld.sum",
    "lts__t_bytes_equiv_l1sectormiss_pipe_lsu_mem_global_op_st.sum",
    "smsp__warps_launched.sum",
    "Duration",
}

DURATION_SCALE = {
    "ns": 1e-9, "nsecond": 1e-9,
    "us": 1e-6, "usecond": 1e-6,
    "ms": 1e-3, "msecond": 1e-3,
    "s": 1.0, "second": 1.0,
}

# ncu reports byte/sector counters with scaled units (Kbyte, Mbyte, …).
# Normalize to base units (bytes / sectors) before summing.
UNIT_SCALE = [
    ("Tbyte",  1e12), ("Tsector", 1e12),
    ("Gbyte",  1e9),  ("Gsector", 1e9),
    ("Mbyte",  1e6),  ("Msector", 1e6),
    ("Kbyte",  1e3),  ("Ksector", 1e3),
    ("byte",   1),    ("sector",  1),
]

# Metrics whose values need unit normalization before summing
SCALED_METRICS = {
    "dram__bytes.sum",
    "dram__bytes_read.sum",
    "dram__bytes_write.sum",
    "lts__t_bytes_lookup_hit.sum",
    "lts__t_bytes_lookup_miss.sum",
    "lts__t_sectors_lookup_hit.sum",
    "lts__t_sectors_lookup_miss.sum",
    "l1tex__t_bytes_lookup_hit.sum",
    "l1tex__t_bytes_lookup_miss.sum",
    "lts__t_bytes_equiv_l1sectormiss_pipe_lsu_mem_global_op_ld.sum",
    "lts__t_bytes_equiv_l1sectormiss_pipe_lsu_mem_global_op_st.sum",
}

def _scale_unit(v, unit_str):
    """Convert a scaled value (e.g. 2.81 Mbyte) to base units (bytes)."""
    u = unit_str.strip()
    for suffix, scale in UNIT_SCALE:
        if suffix in u:
            return v * scale
    return v

def parse_value(val_str):
    v = val_str.strip().replace(",", "")
    if not v or v == "n/a":
        return None
    try:
        return float(v)
    except ValueError:
        return None

def shorten_kernel(name):
    name = re.sub(r'\(.*', '', name)
    if 'copy_map' in name:
        return "cpy:" + name
    name = name.replace('single_state_', 'ss_')
    if 'cub::' in name:
        name = 'cub::Reduce'
    return name.strip()

def _ncu_default_rows(path):
    """Parse default --csv output (one metric per row, col 12 = metric name)."""
    raw = subprocess.run(
        ['ncu', '-i', path, '--csv'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout.decode()
    reader = csv.reader(raw.splitlines())
    next(reader)  # skip header
    rows = []
    for row in reader:
        if len(row) < 15:
            continue
        metric = row[12]
        if not metric.strip() or metric not in ALL_METRIC_NAMES:
            continue
        rows.append(row)
    return rows


def _ncu_raw_rows(path):
    """Parse --page raw output for raw-name metrics missing from default export.

    The raw page puts metrics as column headers (cols 11+), with one row per
    kernel launch.  We pivot this back into the same per-metric row format
    used by the default export (20 columns, metric name in col 12).
    """
    # Collect raw metric names that are in ALL_METRIC_NAMES
    raw_metrics = [m for m in ALL_METRIC_NAMES if '__' in m]
    if not raw_metrics:
        return []
    raw = subprocess.run(
        ['ncu', '-i', path, '--csv', '--page', 'raw',
         '--metrics', ','.join(raw_metrics)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout.decode()
    lines = raw.splitlines()
    if len(lines) < 3:
        return []
    reader = csv.reader(lines)
    header = next(reader)
    units_row = next(reader)  # second row = units

    # Find base columns and metric columns
    # Base: ID(0)..CC(10), metrics start at 11
    metric_cols = {}  # col_index -> (metric_name, unit)
    for ci in range(11, len(header)):
        name = header[ci].strip().strip('"')
        if name in ALL_METRIC_NAMES:
            unit = units_row[ci].strip().strip('"') if ci < len(units_row) else ""
            metric_cols[ci] = (name, unit)

    if not metric_cols:
        return []

    rows = []
    for row in reader:
        if len(row) < 11:
            continue
        # Build base columns matching the default 20-col format
        base = row[:11]  # ID..CC
        for ci, (mname, munit) in metric_cols.items():
            val = row[ci] if ci < len(row) else ""
            # Construct a 20-col row matching default format:
            # 0-10: base, 11: SectionName(empty), 12: MetricName, 13: Unit, 14: Value,
            # 15-19: empty rule fields
            synth = base + ["", mname, munit, val, "", "", "", "", ""]
            rows.append(synth)
    return rows


def parse_file(path):
    default_rows = _ncu_default_rows(path)
    raw_rows = _ncu_raw_rows(path)

    # Deduplicate: if a metric already appeared in default, skip the raw version.
    seen = set()
    for row in default_rows:
        seen.add((row[0], row[12]))  # (launch_id, metric_name)
    for row in raw_rows:
        key = (row[0], row[12])
        if key not in seen:
            default_rows.append(row)
            seen.add(key)

    # First pass: collect the ordered list of (launch_id, kernel_base) pairs.
    rows_by_id = OrderedDict()  # launch_id -> (kernel_base, [(row)])
    all_rows = []
    for row in default_rows:
        metric = row[12]
        launch_id = row[0]
        kernel_base = shorten_kernel(row[4])
        if kernel_base.startswith("cpy:"):
            continue
        all_rows.append((launch_id, kernel_base, row))
        if launch_id not in rows_by_id:
            rows_by_id[launch_id] = kernel_base

    # Build the kernel launch sequence (one entry per launch_id, in order).
    launch_seq = list(rows_by_id.values())  # [kernel_base, kernel_base, ...]

    # Detect the repeating pattern to find global rep boundaries.
    # With --reps=N, the same program runs N times, producing the same
    # kernel launch sequence each time.  Some kernels launch multiple times
    # per run, so the total invocations differ per kernel.
    # Strategy: find period P such that launch_seq[:P] repeats.
    def _find_period(seq):
        n = len(seq)
        for p in range(1, n + 1):
            if n % p != 0:
                continue
            pattern = seq[:p]
            if all(seq[i*p:(i+1)*p] == pattern for i in range(1, n // p)):
                return p
        return n  # no repetition found → treat everything as 1 rep

    period = _find_period(launch_seq)
    n_reps = len(launch_seq) // period if period > 0 else 1
    if period == len(launch_seq) and period > 1:
        raise ValueError(
            f"{path}: could not detect repeating kernel launch pattern "
            f"({len(launch_seq)} launches, no period found). "
            f"Check --reps or kernel filtering."
        )

    # Assign global rep index to each launch_id.
    launch_ids_ordered = list(rows_by_id.keys())
    launch_id_to_rep = {}
    # Track per-kernel sub-invocation within each rep for the data key.
    # Within a single rep, the same kernel can launch multiple times.
    # We use (kernel_base, sub_inv_within_rep) as the effective kernel key.
    launch_id_to_kernel_key = {}
    for rep_idx in range(n_reps):
        sub_inv = defaultdict(int)  # kernel_base -> count within this rep
        for j in range(period):
            lid = launch_ids_ordered[rep_idx * period + j]
            kb = rows_by_id[lid]
            si = sub_inv[kb]
            sub_inv[kb] = si + 1
            launch_id_to_rep[lid] = rep_idx
            # If a kernel launches >1× per rep, disambiguate with sub-index
            if sub_inv[kb] > 1 or si > 0:
                launch_id_to_kernel_key[lid] = f"{kb}#{si}"
            else:
                launch_id_to_kernel_key[lid] = kb

    # Second pass: build data dict with correct global rep indices.
    # (kernel_key, rep, metric) -> (value_str, unit)
    data = OrderedDict()
    for launch_id, kernel_base, row in all_rows:
        if launch_id not in launch_id_to_rep:
            continue
        rep = launch_id_to_rep[launch_id]
        kernel_key = launch_id_to_kernel_key[launch_id]
        metric = row[12]
        data[(kernel_key, rep, metric)] = (row[14], row[13])

    label = os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path)
    return label, path, data

def aggregate(data):
    """Sum raw counters per rep, average across reps.

    Returns (sums_dict, dominant_kernel, n_reps).
    """
    per_rep = defaultdict(lambda: defaultdict(float))
    kernel_dur_per_rep = defaultdict(lambda: defaultdict(float))

    for (kernel, rep, metric), (val_str, unit) in data.items():
        if metric not in SUMMABLE:
            continue
        v = parse_value(val_str)
        if v is None:
            continue
        if metric == "Duration":
            scale = DURATION_SCALE.get(unit.strip(), 1e-6)
            v_sec = v * scale
            per_rep[rep][metric] += v_sec
            kernel_dur_per_rep[rep][kernel] += v_sec
        elif metric in SCALED_METRICS:
            per_rep[rep][metric] += _scale_unit(v, unit)
        else:
            per_rep[rep][metric] += v

    n_reps = len(per_rep) if per_rep else 1
    first_rep = min(per_rep.keys()) if per_rep else 0
    last_rep = max(per_rep.keys()) if per_rep else 0

    # Cold = first rep, warm = last rep
    cold_sums = per_rep.get(first_rep, {})
    warm_sums = per_rep.get(last_rep, {})

    # Dominant kernel per warmth, with duration and percentage
    def _pick_dominant(dur_map):
        if not dur_map:
            return None, 0, 0
        total = sum(dur_map.values())
        dom = max(dur_map, key=dur_map.get)
        pct = dur_map[dom] / total * 100 if total > 0 else 0
        return dom, dur_map[dom], pct

    cold_dur = kernel_dur_per_rep.get(first_rep, {})
    warm_dur = kernel_dur_per_rep.get(last_rep, {})
    dominant_cold, cold_dom_dur, cold_dom_pct = _pick_dominant(cold_dur)
    dominant_warm, warm_dom_dur, warm_dom_pct = _pick_dominant(warm_dur)

    return (cold_sums, warm_sums,
            (dominant_cold, cold_dom_dur, cold_dom_pct),
            (dominant_warm, warm_dom_dur, warm_dom_pct),
            n_reps)

def derive(sums):
    """Derive ratios from summed raw counters.

    A counter the report does not carry yields None, and every quantity
    derived from it is None as well: reporting an uncollected counter as 0
    makes it indistinguishable from a measured zero.
    """
    d = OrderedDict()
    dur = sums.get("Duration", 0)

    def need(*keys):
        """Sum of the named counters, or None if any of them is absent."""
        vals = [sums.get(k) for k in keys]
        return None if any(v is None for v in vals) else vals

    def fma2(fma, mul, add):
        v = need(fma, mul, add)
        return None if v is None else 2 * v[0] + v[1] + v[2]

    # FLOPs
    h = fma2("smsp__sass_thread_inst_executed_op_hfma_pred_on.sum",
             "smsp__sass_thread_inst_executed_op_hmul_pred_on.sum",
             "smsp__sass_thread_inst_executed_op_hadd_pred_on.sum")
    f = fma2("smsp__sass_thread_inst_executed_op_ffma_pred_on.sum",
             "smsp__sass_thread_inst_executed_op_fmul_pred_on.sum",
             "smsp__sass_thread_inst_executed_op_fadd_pred_on.sum")
    dd = fma2("smsp__sass_thread_inst_executed_op_dfma_pred_on.sum",
              "smsp__sass_thread_inst_executed_op_dmul_pred_on.sum",
              "smsp__sass_thread_inst_executed_op_dadd_pred_on.sum")

    d["Total duration [us]"] = dur * 1e6
    d["FP16 FLOPs"] = h
    d["FP32 FLOPs"] = f
    d["FP64 FLOPs"] = dd
    flop_total = None if None in (h, f, dd) else h + f + dd
    d["Total FLOPs"] = flop_total
    if dur > 0:
        d["FP16 GFLOP/s"] = None if h is None else h / dur / 1e9
        d["FP32 GFLOP/s"] = None if f is None else f / dur / 1e9
        d["FP64 GFLOP/s"] = None if dd is None else dd / dur / 1e9
        d["Total GFLOP/s"] = None if flop_total is None else flop_total / dur / 1e9

    dram_rd = sums.get("dram__bytes_read.sum")
    dram_wr = sums.get("dram__bytes_write.sum")
    dram_tot = sums.get("dram__bytes.sum")
    scale = lambda v, k: None if v is None else v / k
    d["DRAM rd [GB]"] = scale(dram_rd, 1e9)
    d["DRAM wr [GB]"] = scale(dram_wr, 1e9)
    d["DRAM total [GB]"] = scale(dram_tot, 1e9)
    if dur > 0:
        d["DRAM rd BW [GB/s]"] = scale(dram_rd, dur * 1e9)
        d["DRAM wr BW [GB/s]"] = scale(dram_wr, dur * 1e9)
        d["DRAM total BW [GB/s]"] = scale(dram_tot, dur * 1e9)

    l2_hit = sums.get("lts__t_sectors_lookup_hit.sum", 0)
    l2_miss = sums.get("lts__t_sectors_lookup_miss.sum", 0)
    if l2_hit + l2_miss > 0:
        d["L2 hit rate [%]"] = 100.0 * l2_hit / (l2_hit + l2_miss)

    l1_hit = sums.get("l1tex__t_bytes_lookup_hit.sum", 0)
    l1_miss = sums.get("l1tex__t_bytes_lookup_miss.sum", 0)
    if l1_hit + l1_miss > 0:
        d["L1 hit rate [%]"] = 100.0 * l1_hit / (l1_hit + l1_miss)

    d["Global ld insns"] = sums.get("smsp__inst_executed_op_global_ld.sum", 0)
    d["Global st insns"] = sums.get("smsp__inst_executed_op_global_st.sum", 0)
    d["Shared ld insns"] = sums.get("smsp__inst_executed_op_shared_ld.sum", 0)
    d["Shared st insns"] = sums.get("smsp__inst_executed_op_shared_st.sum", 0)
    d["Total insns"] = sums.get("smsp__inst_executed.sum", 0)
    d["Int insns"] = sums.get("smsp__inst_executed_op_integer.sum", 0)
    d["FP16 insns"] = sums.get("smsp__inst_executed_op_fp16.sum", 0)
    d["FP32 insns"] = sums.get("smsp__inst_executed_op_fp32.sum", 0)
    d["FP64 insns"] = sums.get("smsp__inst_executed_op_fp64.sum", 0)
    d["Conv insns"] = sums.get("smsp__inst_executed_op_conversion.sum", 0)
    d["Warps launched"] = sums.get("smsp__warps_launched.sum", 0)

    return d

def fmt(val):
    if val is None:
        return "n/a"
    if isinstance(val, float):
        if abs(val) >= 1e6:
            return f"{val:,.0f}"
        elif abs(val) >= 1:
            return f"{val:,.2f}"
        else:
            return f"{val:.4g}"
    return f"{val:,.0f}" if isinstance(val, int) else str(val)

def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(1)

    with ProcessPoolExecutor() as pool:
        results = list(pool.map(parse_file, paths))
    results.sort(key=lambda r: r[0])

    detail_rows = []
    agg_rows = []

    for label, path, data in results:
        # Unique (kernel, rep) pairs in order
        kr_pairs = list(OrderedDict.fromkeys((k, r) for k, r, _ in data.keys()))
        (cold_sums, warm_sums,
         (dom_cold, cold_dur_s, cold_pct),
         (dom_warm, warm_dur_s, warm_pct),
         n_reps) = aggregate(data)
        cold_derived = derive(cold_sums)
        warm_derived = derive(warm_sums)

        # --- Per-kernel detail (all invocations) ---
        print(f"\n{'='*60}")
        print(f"  {label}  ({n_reps} rep{'s' if n_reps != 1 else ''})")
        print(f"{'='*60}")
        for kernel, rep in kr_pairs:
            print(f"\n  [{kernel}] rep={rep}")
            for metric_name, short_name in ALL_METRICS:
                val, unit = data.get((kernel, rep, metric_name), ("n/a", ""))
                print(f"    {short_name:<25s} {val:>15s} {unit}")
                detail_rows.append([label, kernel, rep, short_name, val, unit])

        # --- Aggregate cold + warm ---
        n_kernels = len(set(k for k, _ in kr_pairs))
        for warmth, derived in [("cold", cold_derived), ("warm", warm_derived)]:
            print(f"\n  [AGGREGATE {warmth} across {n_kernels} kernels]")
            for name, val in derived.items():
                print(f"    {name:<25s} {fmt(val):>20s}")
                agg_rows.append([label, "AGGREGATE", warmth, name, fmt(val), ""])

        # --- Dominant kernel cold + warm ---
        for warmth, dom, dur_s, pct in [("cold", dom_cold, cold_dur_s, cold_pct),
                                         ("warm", dom_warm, warm_dur_s, warm_pct)]:
            if dom is None:
                continue
            rep_idx = min(r for k, r in kr_pairs if k == dom) if warmth == "cold" \
                 else max(r for k, r in kr_pairs if k == dom)
            print(f"\n  [DOMINANT {warmth}: {dom}  (rep: {rep_idx}, "
                  f"{dur_s*1e6:.1f} us, {pct:.1f}% of total)]")
            for metric_name, short_name in ALL_METRICS:
                val, unit = data.get((dom, rep_idx, metric_name), ("n/a", ""))
                print(f"    {short_name:<25s} {val:>15s} {unit}")
                agg_rows.append([label, f"DOMINANT:{dom}", warmth, short_name, val, unit])

    with open("ncu_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "kernel", "rep", "metric", "value", "unit"])
        w.writerows(detail_rows)
    print(f"\n>>> Saved per-kernel detail to ncu_summary.csv ({len(detail_rows)} rows)")

    with open("ncu_aggregate.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "kernel", "warmth", "metric", "value", "unit"])
        w.writerows(agg_rows)
    print(f">>> Saved aggregate + dominant to ncu_aggregate.csv ({len(agg_rows)} rows)")

if __name__ == "__main__":
    main()
