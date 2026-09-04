"""Writes .gpkg / .geojson copies of the cane AOI for the AOI-format scenario."""
from pathlib import Path
import geopandas as gpd

SRC = Path("/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_cane.shp")
OUT = Path("/home/jovyan/FAO/optimized_code_testing/aoi_variants")
OUT.mkdir(parents=True, exist_ok=True)

gdf = gpd.read_file(SRC)
gdf.to_file(OUT / "okara_test_data_cane.gpkg", driver="GPKG")
gdf.to_file(OUT / "okara_test_data_cane.geojson", driver="GeoJSON")

for path in [SRC, OUT / "okara_test_data_cane.gpkg", OUT / "okara_test_data_cane.geojson"]:
    g = gpd.read_file(path)
    acres = g.to_crs(32642).area.sum() * 0.000247105
    print(f"{path.name:32s} n={len(g)} crs={g.crs} acres={acres:.2f} "
          f"bounds={[round(v, 6) for v in g.total_bounds]}")
