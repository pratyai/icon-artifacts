"""Quick diagnostic: compare a single field at a few mapped points."""
import argparse
import netCDF4 as nc
import numpy as np
from scipy.spatial import cKDTree
import zstandard as zstd
import re
from pathlib import Path


EDGE_FIELDS = {"vn", "vt", "vn_ie"}

FILE_PAT = re.compile(
    r"^(?P<serde>.+?)\.t0\.p(?P<phys>\d+)\.d(?P<d>\d+)\.vt(?P<vt>\d+)\.ss(?P<ss>\d+)\.data(?:\.zst)?$"
)


def load_grid(path, load_reconstruction=False):
    ds = nc.Dataset(str(path))
    d = {"ncells": ds.dimensions["cell"].size, "nedges": ds.dimensions["edge"].size,
         "clon": ds["clon"][:].data, "clat": ds["clat"][:].data,
         "elon": ds["elon"][:].data, "elat": ds["elat"][:].data}
    if load_reconstruction:
        # Build 3D Cartesian edge normals from geographic (zonal/meridional) normals.
        # Geographic normal (nz, nm) at edge (lon, lat) → Cartesian via local tangent basis:
        #   east  = (-sin(lon), cos(lon), 0)
        #   north = (-sin(lat)*cos(lon), -sin(lat)*sin(lon), cos(lat))
        #   n_cart = nz * east + nm * north
        nz = ds["zonal_normal_primal_edge"][:].data       # (nedges,)
        nm = ds["meridional_normal_primal_edge"][:].data   # (nedges,)
        elon = d["elon"]  # already loaded above
        elat = d["elat"]
        sin_lon, cos_lon = np.sin(elon), np.cos(elon)
        sin_lat, cos_lat = np.sin(elat), np.cos(elat)
        d["enx"] = nz * (-sin_lon) + nm * (-sin_lat * cos_lon)   # (nedges,)
        d["eny"] = nz * cos_lon    + nm * (-sin_lat * sin_lon)   # (nedges,)
        d["enz"] =                   nm * cos_lat                 # (nedges,)
        # Cell-to-edge connectivity and orientation
        d["edge_of_cell"] = ds["edge_of_cell"][:].data           # (3, ncells), 1-based
        d["orient"] = ds["orientation_of_normal"][:].data         # (3, ncells), ±1
        # Parent cell index for fine→coarse mapping (only in fine grids)
        if "parent_cell_index" in ds.variables:
            d["parent_cell_index"] = ds["parent_cell_index"][:].data  # (ncells,), 1-based
    ds.close()
    return d


def reconstruct_vn_to_cells(grid, vn_2d, clon, clat):
    """Reconstruct edge-based vn to cell-center (vx, vy, vz) via tangent-plane LS.

    The 3 edge normals of a triangle are nearly coplanar (tangent to sphere),
    so a full 3×3 solve is rank-deficient in the radial direction.  Instead:
      1. Build a local tangent-plane basis (e1, e2) at each cell center.
      2. Project the 3D Cartesian edge normals into this 2D basis → (3×2) system.
      3. Least-squares solve for 2 tangential components.
      4. Reconstruct back to 3D Cartesian.

    Args:
        grid: dict from load_grid with enx, eny, enz, edge_of_cell
        vn_2d: (nedges, nlev) array of normal velocities
        clon, clat: (ncells,) cell center coordinates in radians

    Returns:
        (vx, vy, vz): each (ncells, nlev) — Cartesian velocity at cell centers
    """
    ncells = grid["ncells"]
    nlev = vn_2d.shape[1]
    enx = grid["enx"]            # (nedges,)
    eny = grid["eny"]            # (nedges,)
    enz = grid["enz"]            # (nedges,)
    eoc = grid["edge_of_cell"]   # (3, ncells), 1-based

    # Edge indices for all cells: (3, ncells) → 0-based
    ei = eoc - 1

    # 3D Cartesian normals for each cell's 3 edges: (ncells, 3, 3)
    N3 = np.stack([enx[ei], eny[ei], enz[ei]], axis=-1).transpose(1, 0, 2)

    # Build local tangent-plane basis at each cell center
    # e1 = east  = (-sin(lon), cos(lon), 0)
    # e2 = north = (-sin(lat)*cos(lon), -sin(lat)*sin(lon), cos(lat))
    sin_lon, cos_lon = np.sin(clon), np.cos(clon)
    sin_lat, cos_lat = np.sin(clat), np.cos(clat)
    # e1, e2: (ncells, 3)
    e1 = np.stack([-sin_lon, cos_lon, np.zeros_like(clon)], axis=-1)
    e2 = np.stack([-sin_lat * cos_lon, -sin_lat * sin_lon, cos_lat], axis=-1)

    # Project 3D normals into tangent plane: N_2d[c, i, :] = [n_i · e1, n_i · e2]
    # N3: (ncells, 3, 3), e1/e2: (ncells, 3) → dots: (ncells, 3)
    n_dot_e1 = np.einsum("cij,cj->ci", N3, e1)  # (ncells, 3)
    n_dot_e2 = np.einsum("cij,cj->ci", N3, e2)  # (ncells, 3)
    N2 = np.stack([n_dot_e1, n_dot_e2], axis=-1)  # (ncells, 3, 2)

    # Least-squares: (N2^T N2)^{-1} N2^T — shape (ncells, 2, 3)
    NtN = np.einsum("cij,cik->cjk", N2, N2)       # (ncells, 2, 2)
    NtN_inv = np.linalg.inv(NtN)                    # (ncells, 2, 2)
    pseudo_inv = np.einsum("cij,ckj->cik", NtN_inv, N2)  # (ncells, 2, 3)

    # Gather vn values: (ncells, 3, nlev)
    vn_cells = vn_2d[ei].transpose(1, 0, 2)

    # Solve for tangent-plane components: (ncells, 2, nlev)
    uv = np.einsum("cij,cjk->cik", pseudo_inv, vn_cells)

    # Reconstruct 3D Cartesian: v = u1 * e1 + u2 * e2
    # uv[:, 0, :] = u1 (ncells, nlev), e1 (ncells, 3)
    vx = uv[:, 0, :] * e1[:, 0:1] + uv[:, 1, :] * e2[:, 0:1]
    vy = uv[:, 0, :] * e1[:, 1:2] + uv[:, 1, :] * e2[:, 1:2]
    vz = uv[:, 0, :] * e1[:, 2:3] + uv[:, 1, :] * e2[:, 2:3]

    return vx, vy, vz  # each (ncells, nlev)


def to_xyz(lon, lat):
    return np.column_stack([np.cos(lat)*np.cos(lon), np.cos(lat)*np.sin(lon), np.sin(lat)])


def parse(path):
    if path.suffix == ".zst":
        text = zstd.ZstdDecompressor().decompress(open(path, "rb").read()).decode("utf-8")
    else:
        text = path.read_text()
    TAG = re.compile(r"^#\s+(\S+)", re.MULTILINE)
    META = {"assoc", "alloc", "rank", "size", "lbound", "entries", "missing"}
    results = {}
    tags = [(m.start(), m.group(1), text.find("\n", m.end()) + 1) for m in TAG.finditer(text)]
    cur = None
    for i, (_, name, ds) in enumerate(tags):
        de = tags[i + 1][0] if i + 1 < len(tags) else len(text)
        if name == "entries" and cur:
            arr = np.fromstring(text[ds:de], sep="\n", dtype=np.float64)
            if arr.size > 0:
                results[cur] = np.concatenate([results[cur], arr]) if cur in results else arr
        elif name not in META:
            cur = name
    return results


def print_timing_metadata(coarse_file, fine_file):
    """Extract and print timing info from serde filenames. Warn on mismatches."""
    for label, path in [("coarse", coarse_file), ("fine", fine_file)]:
        m = FILE_PAT.match(Path(path).name)
        if m:
            print(f"  {label}: serde={m.group('serde')} phys={m.group('phys')} d={m.group('d')} vt={m.group('vt')} ss={m.group('ss')}")
        else:
            print(f"  {label}: could not parse filename {Path(path).name}")
    mc = FILE_PAT.match(Path(coarse_file).name)
    mf = FILE_PAT.match(Path(fine_file).name)
    if mc and mf:
        for key in ["phys", "d", "vt", "ss"]:
            if mc.group(key) != mf.group(key):
                print(f"  WARNING: {key} mismatch! coarse={mc.group(key)}, fine={mf.group(key)}")


def scan_directory(scan_dir, field, nlev):
    """Scan a directory for serde files and report field magnitudes."""
    scan_path = Path(scan_dir)
    files = sorted(scan_path.glob("*.data")) + sorted(scan_path.glob("*.data.zst"))
    entries = []
    for f in files:
        m = FILE_PAT.match(f.name)
        if not m:
            continue
        entries.append((int(m.group("phys")), int(m.group("ss")), m.group("serde"), f))

    entries.sort()
    print(f"Scanning {len(entries)} files for field '{field}' in {scan_dir}")
    print(f"{'serde':<12} {'phys':>4} {'ss':>3} {'max':>12} {'mean':>12} {'nonzero%':>9}")
    print("-" * 60)
    for phys, ss, serde, path in entries:
        data = parse(path)
        if field not in data:
            continue
        arr = data[field]
        abs_arr = np.abs(arr)
        mx = abs_arr.max()
        mn = abs_arr.mean()
        nz_frac = np.sum(abs_arr > 1e-30) / arr.size * 100
        marker = " <---" if mx > 1e-10 else ""
        print(f"{serde:<12} {phys:>4} {ss:>3} {mx:>12.4e} {mn:>12.4e} {nz_frac:>8.1f}%{marker}")


def compare_raw_edges(coarse_grid_path, fine_grid_path, c_arr, f_arr, nlev, n_c_edges, n_f_edges):
    """Compare edge fields directly using parent_edge_index (no reconstruction)."""
    fg_ds = nc.Dataset(str(fine_grid_path))
    if "parent_edge_index" not in fg_ds.variables:
        print("parent_edge_index not found in fine grid — cannot do raw edge comparison")
        fg_ds.close()
        return
    parent_edge_idx = fg_ds["parent_edge_index"][:].data  # 1-based
    fg_ds.close()

    # Build children mapping on edges
    children = {}
    for fine_i, coarse_1 in enumerate(parent_edge_idx):
        coarse_0 = int(coarse_1) - 1
        if coarse_0 < 0:
            continue
        children.setdefault(coarse_0, []).append(fine_i)

    n_per = [len(v) for v in children.values()]
    print(f"\n--- Raw edge comparison (no reconstruction) ---")
    print(f"Edge parent-child: {len(children)} parents, {min(n_per)}-{max(n_per)} children each")

    c_padded = c_arr.size // nlev
    f_padded = f_arr.size // nlev
    c_2d = c_arr.reshape(c_padded, nlev)[:n_c_edges]
    f_2d = f_arr.reshape(f_padded, nlev)[:n_f_edges]

    # Child-averaged comparison (raw vn — sign may differ but magnitude should be comparable)
    f_child_avg = np.zeros_like(c_2d)
    f_child_avg_abs = np.zeros_like(c_2d)
    valid = np.zeros(n_c_edges, dtype=bool)
    for ei in range(n_c_edges):
        ch = children.get(ei)
        if ch:
            f_child_avg[ei] = f_2d[ch].mean(axis=0)
            f_child_avg_abs[ei] = np.abs(f_2d[ch]).mean(axis=0)
            valid[ei] = True

    n_valid = int(valid.sum())

    # Signed comparison
    diff = c_2d[valid] - f_child_avg[valid]
    norm_diff = np.linalg.norm(diff)
    norm_ref = np.linalg.norm(f_child_avg[valid])
    snr = 20 * np.log10(norm_ref / norm_diff) if norm_diff > 0 else float("inf")
    print(f"Signed:   SNR={snr:.1f} dB, RMSE={np.sqrt(np.mean(diff**2)):.6e} ({n_valid} valid edges)")

    # Magnitude comparison (avoids orientation sign issues)
    diff_abs = np.abs(c_2d[valid]) - f_child_avg_abs[valid]
    norm_diff_abs = np.linalg.norm(diff_abs)
    norm_ref_abs = np.linalg.norm(f_child_avg_abs[valid])
    snr_abs = 20 * np.log10(norm_ref_abs / norm_diff_abs) if norm_diff_abs > 0 else float("inf")
    print(f"|vn| avg: SNR={snr_abs:.1f} dB, RMSE={np.sqrt(np.mean(diff_abs**2)):.6e}")

    # Show a few edges
    lev = nlev // 2
    print(f"\nFirst 5 edges at mid-level (lev={lev}):")
    shown = 0
    for ei in range(n_c_edges):
        if not valid[ei]:
            continue
        ch = children[ei]
        c_val = c_2d[ei, lev]
        ch_vals = [f_2d[j, lev] for j in ch[:4]]
        avg_val = f_child_avg[ei, lev]
        print(f"  edge {ei}: coarse={c_val:.6e}, child_avg={avg_val:.6e}, children={[f'{v:.6e}' for v in ch_vals]}")
        shown += 1
        if shown >= 5:
            break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coarse-grid")
    parser.add_argument("--fine-grid")
    parser.add_argument("--coarse-file", help="serde .data[.zst] file")
    parser.add_argument("--fine-file", help="serde .data[.zst] file")
    parser.add_argument("--field", default="w", help="sub-field to compare")
    parser.add_argument("--nlev", type=int, default=90)
    parser.add_argument("--scan", action="store_true", help="scan directory for field magnitudes")
    parser.add_argument("--scan-dir", help="directory to scan (used with --scan)")
    parser.add_argument("--raw-edges", action="store_true", help="compare edge fields directly via parent_edge_index")
    args = parser.parse_args()

    if args.scan:
        scan_dir = args.scan_dir or (args.coarse_file and str(Path(args.coarse_file).parent))
        if not scan_dir:
            parser.error("--scan requires --scan-dir or --coarse-file")
        scan_directory(scan_dir, args.field, args.nlev)
        return

    if not all([args.coarse_grid, args.fine_grid, args.coarse_file, args.fine_file]):
        parser.error("comparison mode requires --coarse-grid, --fine-grid, --coarse-file, --fine-file")

    # Print timing metadata from filenames
    print_timing_metadata(args.coarse_file, args.fine_file)

    is_edge = args.field in EDGE_FIELDS
    cg = load_grid(Path(args.coarse_grid), load_reconstruction=(is_edge and not args.raw_edges))
    fg = load_grid(Path(args.fine_grid), load_reconstruction=(is_edge and not args.raw_edges))

    if is_edge:
        print(f"Edge field '{args.field}' — will reconstruct to cell-center (vx, vy, vz) before comparing")

    # Always compare on cells (edge fields get reconstructed)
    c_xyz = to_xyz(cg["clon"], cg["clat"])
    f_xyz = to_xyz(fg["clon"], fg["clat"])
    n_c, n_f = cg["ncells"], fg["ncells"]
    # Grid sizes for raw edge data
    n_c_raw = cg["nedges"] if is_edge else cg["ncells"]
    n_f_raw = fg["nedges"] if is_edge else fg["ncells"]

    # Use parent_cell_index from fine grid (always cells after reconstruction)
    fg_ds = nc.Dataset(str(Path(args.fine_grid)))
    parent_idx = fg_ds["parent_cell_index"][:].data  # 1-based
    fg_ds.close()

    # Build coarse→fine children mapping
    # parent_idx[fine_i] = coarse_i (1-based)
    children = {}  # coarse_idx (0-based) -> list of fine_idx (0-based)
    for fine_i, coarse_1 in enumerate(parent_idx):
        coarse_0 = coarse_1 - 1  # convert to 0-based
        if coarse_0 not in children:
            children[coarse_0] = []
        children[coarse_0].append(fine_i)

    n_children = [len(children.get(i, [])) for i in range(n_c)]
    print(f"Children per parent: min={min(n_children)}, max={max(n_children)}, typical={n_children[0]}")

    # Also keep KD-tree for comparison
    tree = cKDTree(f_xyz)
    dists, indices = tree.query(c_xyz, k=4)
    inv_d = 1.0 / np.maximum(dists, 1e-30)
    weights = inv_d / inv_d.sum(axis=1, keepdims=True)

    entity = "edge→cell(3D)" if is_edge else "cell"
    print(f"Grid: coarse={n_c} cells ({n_c_raw} raw), fine={n_f} cells ({n_f_raw} raw)")
    print(f"NN distances: min={dists[:,0].min():.4e}, max={dists[:,0].max():.4e}, mean={dists[:,0].mean():.4e}")

    c_data = parse(Path(args.coarse_file))
    f_data = parse(Path(args.fine_file))

    if args.field not in c_data:
        print(f"Field '{args.field}' not in coarse file. Available: {list(c_data.keys())}")
        return
    if args.field not in f_data:
        print(f"Field '{args.field}' not in fine file. Available: {list(f_data.keys())}")
        return

    c_arr = c_data[args.field]
    f_arr = f_data[args.field]
    print(f"Coarse array: {c_arr.size} elements")
    print(f"Fine array: {f_arr.size} elements")

    # Try nlev and nlev+1
    nlev = args.nlev if c_arr.size % args.nlev == 0 else args.nlev + 1

    # Raw edge comparison mode
    if args.raw_edges and is_edge:
        compare_raw_edges(
            Path(args.coarse_grid), Path(args.fine_grid),
            c_arr, f_arr, nlev, cg["nedges"], fg["nedges"],
        )
        return
    elif args.raw_edges and not is_edge:
        print(f"--raw-edges only applies to edge fields ({EDGE_FIELDS}), ignoring")
    c_padded = c_arr.size // nlev
    f_padded = f_arr.size // nlev
    print(f"Using nlev={nlev}, coarse padded_h={c_padded} (grid={n_c_raw}), fine padded_h={f_padded} (grid={n_f_raw})")

    if is_edge:
        # Reshape as edge data, reconstruct to cell centers
        c_edge_2d = c_arr.reshape(c_padded, nlev)[:n_c_raw]
        f_edge_2d = f_arr.reshape(f_padded, nlev)[:n_f_raw]
        print(f"Reconstructing coarse {args.field} ({n_c_raw} edges) → (vx, vy, vz) on {n_c} cells ...")
        c_vx, c_vy, c_vz = reconstruct_vn_to_cells(cg, c_edge_2d, cg["clon"], cg["clat"])
        print(f"Reconstructing fine {args.field} ({n_f_raw} edges) → (vx, vy, vz) on {n_f} cells ...")
        f_vx, f_vy, f_vz = reconstruct_vn_to_cells(fg, f_edge_2d, fg["clon"], fg["clat"])
        # Sanity check: radial component should be ~0 (velocity is tangent to sphere)
        c_r = to_xyz(cg["clon"], cg["clat"])  # (ncells, 3) unit radial vectors
        f_r = to_xyz(fg["clon"], fg["clat"])
        c_vr = c_vx * c_r[:, 0:1] + c_vy * c_r[:, 1:2] + c_vz * c_r[:, 2:3]
        f_vr = f_vx * f_r[:, 0:1] + f_vy * f_r[:, 1:2] + f_vz * f_r[:, 2:3]
        print(f"Radial velocity check (should be ~0):")
        print(f"  coarse: max|vr|={np.abs(c_vr).max():.6e}, mean|vr|={np.abs(c_vr).mean():.6e}")
        print(f"  fine:   max|vr|={np.abs(f_vr).max():.6e}, mean|vr|={np.abs(f_vr).mean():.6e}")

        # Combine into speed for scalar comparison, but also report per-component
        c_2d = np.sqrt(c_vx**2 + c_vy**2 + c_vz**2)
        f_2d = np.sqrt(f_vx**2 + f_vy**2 + f_vz**2)
        # Also store components for detailed reporting
        c_components = {"vx": c_vx, "vy": c_vy, "vz": c_vz, "speed": c_2d}
        f_components = {"vx": f_vx, "vy": f_vy, "vz": f_vz, "speed": f_2d}
    else:
        c_2d = c_arr.reshape(c_padded, nlev)[:n_c]
        f_2d = f_arr.reshape(f_padded, nlev)[:n_f]
        c_components = f_components = None

    print(f"\nCoarse field stats: min={c_2d.min():.6e}, max={c_2d.max():.6e}, mean={c_2d.mean():.6e}")
    print(f"Fine field stats:   min={f_2d.min():.6e}, max={f_2d.max():.6e}, mean={f_2d.mean():.6e}")

    # IDW interpolation
    f_neighbors = f_2d[indices]  # (n_c, 4, nlev)
    f_interp = np.einsum("ij,ijk->ik", weights, f_neighbors)  # (n_c, nlev)

    diff = c_2d - f_interp
    print(f"\nDifference stats: min={diff.min():.6e}, max={diff.max():.6e}, mean={diff.mean():.6e}")
    print(f"RMSE: {np.sqrt(np.mean(diff**2)):.6e}")
    print(f"||coarse||: {np.linalg.norm(c_2d):.6e}")
    print(f"||diff||:   {np.linalg.norm(diff):.6e}")
    snr = 20 * np.log10(np.linalg.norm(f_interp) / np.linalg.norm(diff)) if np.linalg.norm(diff) > 0 else float("inf")
    print(f"SNR: {snr:.1f} dB")

    # Show a few specific points
    lev = nlev // 2
    print(f"\nFirst 5 coarse points at mid-level (lev={lev}):")
    for i in range(5):
        c_val = c_2d[i, lev]
        f_val = f_interp[i, lev]
        nn_val = f_2d[indices[i, 0], lev]
        print(f"  cell {i}: coarse={c_val:.6e}, fine_interp={f_val:.6e}, fine_nn={nn_val:.6e}, diff={c_val-f_val:.6e}")

    # Parent-child averaging comparison
    print(f"\n--- Parent-child average comparison ---")
    f_child_avg = np.zeros_like(c_2d)
    for ci in range(n_c):
        ch = children.get(ci, [])
        if ch:
            f_child_avg[ci] = f_2d[ch].mean(axis=0)
    diff_pc = c_2d - f_child_avg
    norm_pc = np.linalg.norm(diff_pc)
    norm_ref = np.linalg.norm(f_child_avg)
    snr_pc = 20 * np.log10(norm_ref / norm_pc) if norm_pc > 0 else float("inf")
    print(f"RMSE: {np.sqrt(np.mean(diff_pc**2)):.6e}")
    print(f"||diff||: {norm_pc:.6e}")
    print(f"SNR (parent-child avg): {snr_pc:.1f} dB")

    print(f"\nFirst 5 coarse points at mid-level (parent-child avg):")
    for i in range(5):
        ch = children.get(i, [])
        c_val = c_2d[i, lev]
        pc_val = f_child_avg[i, lev]
        ch_vals = [f_2d[j, lev] for j in ch[:4]]
        print(f"  cell {i}: coarse={c_val:.6e}, child_avg={pc_val:.6e}, children={[f'{v:.6e}' for v in ch_vals]}")

    # Find cells with largest coarse values
    print(f"\n5 largest |coarse| cells at mid-level:")
    c_lev = c_2d[:, lev]
    top5 = np.argsort(np.abs(c_lev))[-5:][::-1]
    for i in top5:
        c_val = c_2d[i, lev]
        f_val = f_interp[i, lev]
        nn_val = f_2d[indices[i, 0], lev]
        d = dists[i, 0]
        print(f"  cell {i} (dist={d:.4e}): coarse={c_val:.6e}, fine_interp={f_val:.6e}, fine_nn={nn_val:.6e}")

    # Distribution of zeros
    n_zero = np.sum(np.abs(c_lev) < 1e-30)
    print(f"\nZero cells at lev={lev}: {n_zero}/{n_c}")
    nonzero = c_lev[np.abs(c_lev) > 1e-30]
    if nonzero.size > 0:
        print(f"Non-zero range: [{nonzero.min():.6e}, {nonzero.max():.6e}]")
    else:
        print("All values are zero at this level.")

    # Per-component SNR for reconstructed edge fields
    if c_components is not None:
        print(f"\n--- Per-component parent-child SNR ---")
        for comp in ["vx", "vy", "vz", "speed"]:
            cc = c_components[comp]
            fc = f_components[comp]
            fc_avg = np.zeros_like(cc)
            for ci in range(n_c):
                ch = children.get(ci, [])
                if ch:
                    fc_avg[ci] = fc[ch].mean(axis=0)
            d = cc - fc_avg
            nd = np.linalg.norm(d)
            nr = np.linalg.norm(fc_avg)
            snr_c = 20 * np.log10(nr / nd) if nd > 0 else float("inf")
            print(f"  {comp:>5s}: SNR={snr_c:.1f} dB, RMSE={np.sqrt(np.mean(d**2)):.6e}")
    else:
        # Check if data might be transposed (nlev, n_h) instead of (n_h, nlev)
        c_2d_T = c_arr.reshape(c_padded, nlev)[:n_c].T  # (nlev, n_c)
        print(f"\nTransposed check: first 5 cells at lev=0:")
        for i in range(5):
            print(f"  normal: c_2d[{i},0]={c_2d[i,0]:.6e}, transposed: c_2d_T[0,{i}]={c_2d_T[0,i]:.6e}")


if __name__ == "__main__":
    main()
