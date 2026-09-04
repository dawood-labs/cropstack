"""Field-log fixes: messy / empty / non-polygon AOIs must fail clearly or be repaired."""
import sys, traceback
sys.path.insert(0, "/home/jovyan/FAO/optimized_code_testing/cropstack")
import geopandas as gpd, pandas as pd
from shapely.geometry import Polygon, Point, LineString, MultiPolygon, box
from shapely.ops import unary_union

OUT = "/home/jovyan/FAO/optimized_code_testing/aoi_variants/messy"
REAL = "/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_cane.shp"
real = gpd.read_file(REAL).to_crs(4326)
minx, miny, maxx, maxy = real.total_bounds

def save(name, geoms, crs=4326):
    p = f"{OUT}/{name}.gpkg"
    gpd.GeoDataFrame({"id": range(len(geoms))}, geometry=geoms, crs=crs).to_file(p, driver="GPKG")
    return p

# 1. self-intersecting bow-tie overlapping the real AOI
bowtie = Polygon([(minx, miny), (maxx, maxy), (minx, maxy), (maxx, miny)])
p_bowtie = save("bowtie_selfintersect", [bowtie])
# 2. mixed geometry: a valid polygon + a line + a point, all in one layer
mixed = [box(minx, miny, (minx+maxx)/2, (miny+maxy)/2),
         LineString([(minx, miny), (maxx, maxy)]),
         Point((minx+maxx)/2, (miny+maxy)/2)]
p_mixed = save("mixed_geometry", mixed)
# 3. real AOI made invalid: bow-tie unioned with the genuine polygon
# real AOI polygon AND a self-intersecting bow-tie in the same layer
p_real_messy = save("real_plus_bowtie", [real.union_all(), bowtie])
# 4. empty layer
p_empty = f"{OUT}/empty_aoi.gpkg"
gpd.GeoDataFrame({"id": []}, geometry=[], crs=4326).to_file(p_empty, driver="GPKG")
# 5. points only
p_points = save("points_only", [Point(minx, miny), Point(maxx, maxy)])

from gee_client import split_aoi_into_grid
import aoi_io

CASES = [("bow-tie (self-intersecting)", p_bowtie),
         ("mixed polygon+line+point", p_mixed),
         ("real AOI + bow-tie", p_real_messy),
         ("EMPTY layer", p_empty),
         ("points only", p_points)]

print("="*78); print("A. grid split (gee_client.split_aoi_into_grid)"); print("="*78)
for label, path in CASES:
    try:
        slug = "".join(ch if ch.isalnum() else "_" for ch in label)[:24]
        slug = "".join(ch if ch.isalnum() else "_" for ch in label)[:24]
        slug = "".join(ch if ch.isalnum() else "_" for ch in label)[:24]
        res = split_aoi_into_grid(path, "grid", grid_cell_acres=15000,
                                  output_dir=f"{OUT}/work_{slug}")
        n = len(gpd.read_file(res["gridded_aoi"]))
        print(f"  {label:32s} -> OK, {n} grid cell(s)")
    except BaseException as exc:
        msg = str(exc).split("\n")[0][:150]
        bad = any(t in type(exc).__name__ for t in ("AttributeError","TypeError","KeyError"))
        print(f"  {label:32s} -> {type(exc).__name__}: {msg}")
        if bad: print(f"      ^^ LOW-QUALITY ERROR (NoneType/schema-style), not a clear message")

print(); print("="*78); print("B. aoi_io.resolve_aoi(verify_readable=True)"); print("="*78)
for label, path in CASES + [("GeoParquet", "/home/jovyan/FAO/optimized_code_testing/aoi_variants/okara_test_data_cane.parquet")]:
    try:
        r = aoi_io.resolve_aoi(path, verify_readable=True)
        g = gpd.read_file(r)
        print(f"  {label:32s} -> OK, n={len(g)}, crs={g.crs}, types={sorted(set(g.geom_type))}")
    except BaseException as exc:
        print(f"  {label:32s} -> {type(exc).__name__}: {str(exc).split(chr(10))[0][:150]}")

print(); print("="*78); print("C. STAC path: config build + validate + tile split"); print("="*78)
from config import build_pipeline_config
KEY = "/home/jovyan/FAO/optimized_code_testing/gcs_data_downloader_ee_farmdar.json"
for label, path in CASES:
    try:
        cfg = build_pipeline_config("cane", "2025", "okara", aoi_path=path,
                                    gee_service_account_key=KEY, ndvi_source="stac",
                                    static_source="stac", run_static_model=False)
        cfg.validate()
        from farmdar.sentinel import _load_aoi_geometry, _build_tiles
        geom = _load_aoi_geometry(cfg.aoi_path)
        tiles = _build_tiles(geom, cfg.stac_tile_size_deg, 8.983e-5)
        print(f"  {label:32s} -> config OK, tile_aoi -> {len(tiles)} tile(s)")
    except BaseException as exc:
        print(f"  {label:32s} -> {type(exc).__name__}: {str(exc).split(chr(10))[0][:130]}")
