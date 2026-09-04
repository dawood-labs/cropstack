"""Does a resumed run's returned GPKG actually correspond to the static raster that
run produced?

C2 (run_mode="resume") resumed vector run 2, whose GPKG had been written by A2 from the
18-Oct+10-Nov static mosaic. C2 itself recomputed the static stage in auto mode and
produced the 16-Oct mosaic -- but `vectorize_process_and_export` short-circuits on
"does <basename>.gpkg exist", so it returned A2's polygons unchanged.
"""
import geopandas as gpd
import numpy as np
import rasterio
from pathlib import Path

W = Path("runs/A1_cane_2025")
ACRES = 0.000247105


def raster_acres(path):
    with rasterio.open(path) as src:
        data = src.read(1)
    return float((data == 1).sum()) * 100 * ACRES


def gpkg_acres(path):
    gdf = gpd.read_file(path)
    return len(gdf), float(gdf.to_crs(32642).area.sum() * ACRES)


print("static rasters produced in this workspace")
for path in sorted(W.glob("2_static_run_*/**/*_Cls_sieved_p20.tif")):
    print(f"  {str(path.relative_to(W)):<75} class-1 acres = {raster_acres(path):9.1f}")

print("\nvector products")
for path in sorted(W.glob("3_vector_run_*/final_output/*.gpkg")):
    n, acres = gpkg_acres(path)
    mtime = path.stat().st_mtime
    print(f"  {str(path.relative_to(W)):<75} {n:5d} features  {acres:9.1f} acres  mtime={mtime:.0f}")

print("\nwhat each run reported as its own outputs")
import json
for name in ("A1_cane_2025", "A2_cane_2025", "C2_second_resume", "C6_ndvi_resume_static_new"):
    p = Path("metrics") / f"{name}_result.json"
    if not p.exists():
        continue
    o = json.loads(p.read_text()).get("outcome") or {}
    sr = o.get("sieved_static_raster")
    vo = o.get("vector_output")
    if not sr or not vo:
        print(f"  {name}: incomplete outcome")
        continue
    r_acres = raster_acres(sr)
    n, v_acres = gpkg_acres(vo)
    ratio = v_acres / r_acres if r_acres else float("nan")
    flag = "  <-- MISMATCH" if ratio < 0.15 or ratio > 1.05 else ""
    print(f"  {name:<28} static={Path(sr).parent.name:<32} raster={r_acres:8.1f} ac"
          f"   vector={v_acres:8.1f} ac  ({ratio:.2f}x){flag}")
