"""Controlled comparison of the two NDVI backends, inside the AOI polygon.

A3_cane_2025 (ndvi=stac) and G_gee_ndvi_cane_2025 (ndvi=gee) consume the SAME
GEE static image (identical auto dates 2025-11-10 + 2025-11-09), so any product
difference between them is attributable to the NDVI backend alone.

Note on extents: the STAC NDVI grid is 2226x2226 because acquisition pads the AOI
bbox out to whole 0.1-deg tiles; the GEE grid is 2000x1625, tight to the AOI bbox.
Both cover 100% of the AOI polygon, so all comparison is done inside the polygon.
"""
import numpy as np, rasterio, geopandas as gpd
from rasterio.warp import reproject, Resampling
from rasterio.features import rasterize

ROOT = "/home/jovyan/FAO/optimized_code_testing"
STAC = f"{ROOT}/runs/A1_cane_2025/1_ndvi_run_1/okara_test_data_cane_rf_classification_map_sieved_p20.tif"
GEE  = f"{ROOT}/runs/G_geendvi_cane_2025/1_ndvi_run_1/okara_test_data_cane_rf_classification_map_sieved_p20.tif"

aoi = gpd.read_file(f"{ROOT}/test_aois_small/okara_test_data_cane.shp").to_crs(4326)
poly = aoi.union_all() if hasattr(aoi, "union_all") else aoi.unary_union

with rasterio.open(STAC) as s1:
    a1, t1, w, h, crs = s1.read(1), s1.transform, s1.width, s1.height, s1.crs
with rasterio.open(GEE) as s2:
    a2, t2, crs2 = s2.read(1), s2.transform, s2.crs

# GEE product onto the STAC grid, nearest-neighbour (both 0.00008983 deg)
g = np.full((h, w), 255, dtype=a2.dtype)
reproject(a2, g, src_transform=t2, src_crs=crs2, dst_transform=t1, dst_crs=crs,
          resampling=Resampling.nearest, src_nodata=255, dst_nodata=255)

in_aoi = rasterize([(poly, 1)], out_shape=(h, w), transform=t1, dtype="uint8").astype(bool)
print(f"AOI pixels on the 10 m grid: {in_aoi.sum():,}")

c1, c2 = (a1 == 1) & in_aoi, (g == 1) & in_aoi
nod = (g == 255) & in_aoi
both, o1, o2 = int((c1 & c2).sum()), int((c1 & ~c2).sum()), int((c2 & ~c1).sum())
print(f"\nclass-1 px in AOI   STAC {int(c1.sum()):>8,}   GEE {int(c2.sum()):>8,}"
      f"   ({100*(c2.sum()-c1.sum())/c1.sum():+.1f}%)")
print(f"agreement           both {both:,}   STAC-only {o1:,}   GEE-only {o2:,}   IoU {both/(both+o1+o2):.3f}")
print(f"GEE nodata in AOI   {int(nod.sum()):,} px = {100*nod.sum()/in_aoi.sum():.2f}% of the AOI")
print(f"  of those, STAC classed crop: {int((nod & c1).sum()):,} px")
