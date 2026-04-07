"""Spatial convergence comparison across ICON grid refinements.

Reads fields from serde .data/.data.zst files at two grid resolutions,
maps coarse-grid points to their corresponding fine-grid points using
coordinate matching from grid NetCDF files, and computes pairwise SNR.

Usage:
    python compare_spatial_v3.py \
        --grids icon_grid_0050_R02B03_G.nc icon_grid_0010_R02B04_G.nc \
        --data-dirs experiments/R02B03 experiments/R02B04 \
        --ss 5 --phys 21 \
        -o spatial_convergence.db
"""
import argparse
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import netCDF4 as nc
import numpy as np
import zstandard as zstd
from tqdm import tqdm

DEFAULT_WORKERS = int(
    os.environ.get(
        "SLURM_CPUS_PER_TASK", os.environ.get("SLURM_CPUS_ON_NODE", os.cpu_count() or 1)
    )
)

# ---------------------------------------------------------------------------
# Grid coordinate mapping
# ---------------------------------------------------------------------------

def load_grid_coords(nc_path: Path) -> dict:
    """Load cell and edge center coordinates, areas and orientations from an ICON grid file."""
    ds = nc.Dataset(str(nc_path))
    coords = {
        "ncells": ds.dimensions["cell"].size,
        "nedges": ds.dimensions["edge"].size,
        "clon": ds["clon"][:].data.copy(),
        "clat": ds["clat"][:].data.copy(),
        "elon": ds["elon"][:].data.copy(),
        "elat": ds["elat"][:].data.copy(),
    }
    if "edge_system_orientation" in ds.variables:
        coords["edge_orient"] = ds["edge_system_orientation"][:].data.copy()
    else:
        coords["edge_orient"] = np.ones(coords["nedges"])
    ds.close()
    return coords


def build_parent_child_mapping(fine_nc_path: Path, entity: str) -> Dict[int, List[int]]:
    """Build coarse→fine children mapping using parent_*_index from fine grid file.

    Returns:
        children: {coarse_idx_0based: [fine_idx_0based, ...]}
    """
    ds = nc.Dataset(str(fine_nc_path))
    if entity == "cell":
        parent_idx = ds["parent_cell_index"][:].data  # 1-based
    elif entity == "edge":
        parent_idx = ds["parent_edge_index"][:].data   # 1-based
    else:
        raise ValueError(f"Unknown entity: {entity}")
    ds.close()

    children: Dict[int, List[int]] = {}
    for fine_i, coarse_1 in enumerate(parent_idx):
        coarse_0 = int(coarse_1) - 1  # convert to 0-based
        if coarse_0 < 0: continue
        if coarse_0 not in children:
            children[coarse_0] = []
        children[coarse_0].append(fine_i)

    n_parents = len(children)
    n_per = [len(v) for v in children.values()]
    print(f"  {entity} parent-child: {n_parents} parents, {min(n_per)}-{max(n_per)} children each")
    return children


# ---------------------------------------------------------------------------
# File I/O (reused from compare_got_and_want_v3)
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
    results: Dict[str, np.ndarray] = {}
    tags = []
    for m in _TAG_RE.finditer(text):
        eol = text.find("\n", m.end())
        data_start = eol + 1 if eol != -1 else len(text)
        tags.append((m.start(), m.group(1), data_start))

    if not tags:
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


# ---------------------------------------------------------------------------
# Field → entity mapping
# ---------------------------------------------------------------------------

# Which grid entity each sub-field lives on
FIELD_ENTITY = {
    # p_prog
    "vn": "edge",      # normal velocity on edges
    "w": "cell",       # vertical velocity on cells
    # p_diag
    "vn_ie": "edge",   # vn at half levels on edges
    "vt": "edge",      # tangential velocity on edges
    "w_concorr_c": "cell",  # contravariant correction on cells
}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_mapped(
    coarse_arr: np.ndarray,
    fine_arr: np.ndarray,
    children: Dict[int, List[int]],
    nlev: int,
    n_coarse_h: int,
    n_fine_h: int,
) -> Dict[str, Any]:
    """Compare coarse field against child-averaged fine field.

    Arrays are flat serde output. Reshape to (padded_h, nlev), slice to grid size.
    """
    if not children:
        return {"ok": False, "error": "No children mapping", "count": 0,
                "n_points": 0, "SNR_db": -1.0, "vSNR_db": -1.0, "max_abs": -1.0, "rmse": -1.0}

    n_coarse_padded = coarse_arr.size // nlev
    n_fine_padded = fine_arr.size // nlev
    c_2d = coarse_arr.reshape(n_coarse_padded, nlev)[:n_coarse_h]
    f_2d = fine_arr.reshape(n_fine_padded, nlev)[:n_fine_h]

    # Average children for each coarse point
    f_avg = np.zeros_like(c_2d)
    valid_mask = np.zeros(n_coarse_h, dtype=bool)
    for ci in range(n_coarse_h):
        ch = children.get(ci)
        if ch:
            f_avg[ci] = f_2d[ch].mean(axis=0)
            valid_mask[ci] = True

    n_valid = int(np.sum(valid_mask))
    c_flat = c_2d[valid_mask].ravel()
    f_flat = f_avg[valid_mask].ravel()

    abs_diff = np.abs(c_flat - f_flat)
    norm_diff = np.linalg.norm(abs_diff)
    norm_fine = np.linalg.norm(f_flat)

    if norm_diff == 0:
        snr_db = float("inf")
    elif norm_fine == 0:
        snr_db = -float("inf")
    else:
        snr_db = 20 * np.log10(norm_fine / norm_diff)

    var_fine = np.var(f_flat)
    var_err = np.var(c_flat - f_flat)
    if var_err == 0:
        vsnr_db = float("inf")
    elif var_fine == 0:
        vsnr_db = -float("inf")
    else:
        vsnr_db = 10 * np.log10(var_fine / var_err)

    return {
        "ok": True,
        "count": int(c_flat.size),
        "n_points": int(n_valid),
        "SNR_db": float(snr_db),
        "vSNR_db": float(vsnr_db),
        "max_abs": float(np.max(abs_diff)),
        "rmse": float(norm_diff / np.sqrt(c_flat.size)),
    }


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS spatial_comparisons (
    phys            INTEGER NOT NULL,
    ss              INTEGER NOT NULL,
    coarse_grid     TEXT NOT NULL,
    fine_grid       TEXT NOT NULL,
    field           TEXT NOT NULL,
    sub_field       TEXT NOT NULL,
    n_points        INTEGER,
    total_elements  INTEGER,
    SNR_db          REAL,
    vSNR_db         REAL,
    max_abs         REAL,
    rmse            REAL,
    PRIMARY KEY (phys, ss, coarse_grid, fine_grid, field, sub_field)
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def row_exists(conn, phys, ss, coarse_grid, fine_grid, field, sub_field) -> bool:
    return conn.execute(
        "SELECT 1 FROM spatial_comparisons WHERE phys=? AND ss=? AND coarse_grid=? AND fine_grid=? AND field=? AND sub_field=? LIMIT 1",
        (phys, ss, coarse_grid, fine_grid, field, sub_field),
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

SKIP_FIELDS = {"dtime", "istep", "ntnd", "lvn_only", "ldeepatmo", "dt_linintp_ubc",
               "global_data", "p_patch", "p_int", "p_metrics",
               "z_kin_hor_e", "z_vt_ie", "z_w_concorr_me"}

FILE_PAT = re.compile(
    r"^(?P<field>.+?)\.t0\.p(?P<phys>\d+)\.d(?P<d>\d+)\.vt(?P<vt>\d+)\.ss(?P<ss>\d+)\.data(?:\.zst)?$"
)


def discover_files(d: Path, ss: int, phys: Optional[int] = None) -> Dict[Tuple[int, str], Path]:
    """Returns {(phys, field): path} for given ss."""
    out = {}
    for f in d.iterdir():
        m = FILE_PAT.match(f.name)
        if not m:
            continue
        if int(m.group("d")) != 1:
            continue
        if int(m.group("ss")) != ss:
            continue
        field = m.group("field")
        if field.split(".")[0] in SKIP_FIELDS:
            continue
        p = int(m.group("phys"))
        if phys is not None and p != phys:
            continue
        out[(p, field)] = f
    return out


def _extract_grid_label(nc_path: str) -> str:
    m = re.search(r"(R\d+B\d+)", nc_path)
    return m.group(1) if m else Path(nc_path).stem


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Spatial convergence comparison across ICON grid refinements."
    )
    parser.add_argument("--grids", nargs="+", required=True,
                        help="Grid NC files, coarsest to finest")
    parser.add_argument("--data-dirs", nargs="+", required=True,
                        help="Data directories, matching --grids order")
    parser.add_argument("--ss", type=int, nargs="+", default=[5, 10],
                        help="Substep values to compare (default: 5 10)")
    parser.add_argument("-p", "--phys", type=int, nargs="*", default=None,
                        help="Physics steps (default: all common)")
    parser.add_argument("-o", "--output", default="spatial_convergence.db")
    parser.add_argument("--nlev", type=int, default=90, help="Number of vertical levels")
    parser.add_argument("--nproma", type=int, default=0, help="NPROMA block size (0 = auto-detect per grid)")
    args = parser.parse_args()

    assert len(args.grids) == len(args.data_dirs), "Must have same number of grids and data dirs"

    # Load all grid coordinates
    grids = []
    for nc_path, data_dir in zip(args.grids, args.data_dirs):
        label = _extract_grid_label(nc_path)
        coords = load_grid_coords(Path(nc_path))
        grids.append({
            "label": label,
            "nc_path": nc_path,
            "data_dir": Path(data_dir),
            "coords": coords,
        })
    print(f"Grids: {[g['label'] for g in grids]}")
    print(f"Cells: {[g['coords']['ncells'] for g in grids]}")
    print(f"Edges: {[g['coords']['nedges'] for g in grids]}")

    # Build pairwise mappings (coarse → fine)
    # Build parent-child mappings for consecutive grid pairs only
    # (parent_cell_index in B(k+1) points to B(k), not B(k-1))
    mappings = {}  # (coarse_label, fine_label, entity) -> children dict
    for i in range(len(grids) - 1):
        coarse, fine = grids[i], grids[i + 1]
        print(f"\nBuilding mapping {coarse['label']} -> {fine['label']}:")
        for entity in ["cell", "edge"]:
            key = (coarse["label"], fine["label"], entity)
            mappings[key] = build_parent_child_mapping(
                Path(fine["nc_path"]), entity
            )

    conn = init_db(Path(args.output))

    # Discover all files across all ss values and grids
    # Key: (grid_label, ss, phys, field) -> parsed data
    parsed_cache: Dict[Path, Dict[str, np.ndarray]] = {}

    def get_parsed(path: Path) -> Dict[str, np.ndarray]:
        if path not in parsed_cache:
            import tempfile, hashlib
            cache_root = Path(tempfile.gettempdir()) / "spatial_v3_cache"
            rel_hash = hashlib.md5(str(path.absolute()).encode()).hexdigest()
            cache_dir = cache_root / rel_hash

            if cache_dir.exists():
                results = {}
                for npy_f in cache_dir.glob("*.npy"):
                    results[npy_f.stem] = np.load(npy_f)
                if results:
                    parsed_cache[path] = results
                    return results

            results = parse_serde_file(path)
            if results:
                cache_dir.mkdir(parents=True, exist_ok=True)
                for name, arr in results.items():
                    np.save(cache_dir / f"{name}.npy", arr)
            parsed_cache[path] = results
        return parsed_cache[path]

    for ss in args.ss:
        print(f"\n--- ss={ss} ---")

        # Discover files per grid
        grid_files = {}
        for g in grids:
            gf = discover_files(g["data_dir"], ss)
            grid_files[g["label"]] = gf
            print(f"  {g['label']}: {len(gf)} files")

        # Common phys steps
        all_phys_sets = [set(k[0] for k in gf) for gf in grid_files.values()]
        common_phys = sorted(set.intersection(*all_phys_sets))
        if args.phys:
            common_phys = [p for p in common_phys if p in args.phys]
        print(f"  Common phys steps: {len(common_phys)}")

        # Common fields
        all_field_sets = [set(k[1] for k in gf) for gf in grid_files.values()]
        common_fields = sorted(set.intersection(*all_field_sets))
        print(f"  Common fields: {common_fields}")

        # Compare consecutive grid pairs only
        for i in range(len(grids) - 1):
            j = i + 1
            coarse, fine = grids[i], grids[j]
            cl, fl = coarse["label"], fine["label"]
            print(f"\n  Comparing {cl} vs {fl}:")
            
            tasks = [(p, field) for p in common_phys for field in common_fields]
            pbar = tqdm(tasks, desc=f"  {cl}→{fl} ss={ss}", unit="file")

            batch = []
            for phys, field in pbar:
                pbar.set_postfix_str(f"p{phys} {field}")

                c_path = grid_files[cl][(phys, field)]
                f_path = grid_files[fl][(phys, field)]

                c_data = get_parsed(c_path)
                f_data = get_parsed(f_path)

                all_vars = sorted(set(c_data) & set(f_data))
                for var in all_vars:
                    # Detect entity by size
                    c_size = c_data[var].size
                    f_size = f_data[var].size
                    
                    # Try nlev and nlev+1
                    nlev = None
                    for try_nlev in [args.nlev, args.nlev + 1]:
                        if c_size % try_nlev == 0 and f_size % try_nlev == 0:
                            nlev = try_nlev
                            break
                    if nlev is None:
                        tqdm.write(f"    SKIP {var}: incompatible size {c_size}, {f_size}")
                        continue
                        
                    c_h = c_size // nlev
                    f_h = f_size // nlev
                    
                    # Match to grid entity
                    entity = None
                    if abs(c_h - coarse["coords"]["ncells"]) < abs(c_h - coarse["coords"]["nedges"]):
                        entity = "cell"
                    else:
                        entity = "edge"
                    
                    map_key = (cl, fl, entity)
                    if map_key not in mappings:
                        continue

                    children_map = mappings[map_key]
                    n_h_coarse = coarse["coords"][f"n{entity}s"]
                    n_h_fine = fine["coords"][f"n{entity}s"]

                    if c_data[var].size // nlev < n_h_coarse or f_data[var].size // nlev < n_h_fine:
                        tqdm.write(f"    SKIP {var}: horizontal count too small")
                        continue

                    stats = compare_mapped(
                        c_data[var], f_data[var], children_map,
                        nlev, n_h_coarse, n_h_fine,
                    )

                    batch.append({
                        "phys": phys,
                        "ss": ss,
                        "coarse_grid": cl,
                        "fine_grid": fl,
                        "field": field,
                        "sub_field": var,
                        "n_points": stats.get("n_points", -1),
                        "total_elements": stats.get("count", -1),
                        "SNR_db": stats["SNR_db"],
                        "vSNR_db": stats["vSNR_db"],
                        "max_abs": stats.get("max_abs", -1.0),
                        "rmse": stats.get("rmse", -1.0),
                    })

                if len(batch) >= 50:
                    conn.executemany(
                        "INSERT OR REPLACE INTO spatial_comparisons VALUES "
                        "(:phys,:ss,:coarse_grid,:fine_grid,:field,:sub_field,"
                        ":n_points,:total_elements,:SNR_db,:vSNR_db,:max_abs,:rmse)",
                        batch,
                    )
                    conn.commit()
                    batch = []

            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO spatial_comparisons VALUES "
                    "(:phys,:ss,:coarse_grid,:fine_grid,:field,:sub_field,"
                    ":n_points,:total_elements,:SNR_db,:vSNR_db,:max_abs,:rmse)",
                    batch,
                )
                conn.commit()

    # Print summary
    rows = conn.execute(
        "SELECT phys, coarse_grid, fine_grid, ss, field, sub_field, "
        "ROUND(SNR_db, 1), ROUND(vSNR_db, 1), ROUND(rmse, 6) "
        "FROM spatial_comparisons "
        "ORDER BY phys, coarse_grid, fine_grid, ss, field, sub_field"
    ).fetchall()
    if rows:
        import polars as pl
        df = pl.DataFrame(rows, schema=[
            "phys", "coarse", "fine", "ss", "field", "sub_field",
            "SNR", "vSNR", "rmse",
        ], orient="row")
        print("\n" + "=" * 80)
        print("SPATIAL CONVERGENCE SUMMARY")
        print("=" * 80)
        with pl.Config(tbl_rows=100, tbl_width_chars=200, tbl_cols=-1, float_precision=1):
            print(df)

    conn.close()
    print(f"\nResults saved to: {args.output}")


if __name__ == "__main__":
    main()
