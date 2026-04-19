#!/usr/bin/env python3
"""Print high-level stats for ICON grid NetCDF files."""

import glob
import sys

import netCDF4 as nc
import numpy as np


def grid_stats(patterns: list[str]):
    files = sorted(f for p in patterns for f in glob.glob(p))
    if not files:
        print("No grid files found.")
        sys.exit(1)

    print(
        f"{'Grid':<16} {'Cells':>10} {'Edges':>10} {'Verts':>10}"
        f" {'Mean dx (km)':>14} {'Mean edge (km)':>15} {'Min edge (km)':>14} {'Max edge (km)':>14}"
    )
    print("-" * 108)

    prev_cells = None
    for f in files:
        ds = nc.Dataset(f)
        ncells = ds.dimensions["cell"].size
        nedges = ds.dimensions["edge"].size
        nverts = ds.dimensions["vertex"].size
        name = f.rsplit("/", 1)[-1].replace("icon_grid_", "").replace("_G.nc", "")

        area_m2 = ds.variables["cell_area"][:]
        mean_dx_km = np.sqrt(area_m2.mean()) / 1000

        el_m = ds.variables["edge_length"][:]

        ratio = f"  ({prev_cells / ncells:.0f}x prev)" if prev_cells else ""
        print(
            f"{name:<16} {ncells:>10,} {nedges:>10,} {nverts:>10,}"
            f" {mean_dx_km:>14.1f} {el_m.mean()/1000:>15.1f} {el_m.min()/1000:>14.1f} {el_m.max()/1000:>14.1f}"
            f"{ratio}"
        )
        prev_cells = ncells
        ds.close()


if __name__ == "__main__":
    patterns = sys.argv[1:] or ["icon_grid_*.nc"]
    grid_stats(patterns)
