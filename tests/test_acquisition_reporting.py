"""Per-tile acquisition cost, and the two all-nodata refusals.

R2-5: `min/tile` used to be wall-clock over tile count, which eight workers divide by
eight -- the 2.6 min/tile measured on a real Okara run reported as 0.8, and no threshold
worth setting could fire. The numbers below are the ones actually measured on that run
(farmdar reported 121.2 / 136.2 / 181.1 / 196.7 s for the four tiles in 3.3 min wall).

R2-3: an all-nodata raster is a failed acquisition, not a district without crop, and the
two must not produce the same output.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

import ndvi_pipeline
import postprocess
from config import PipelineConfig

# The real W5_cane_fresh_1016 acquisition.
REAL_TILE_SECONDS = [121.2, 136.2, 181.1, 196.7]
REAL_WALL_MINUTES = 3.3
REAL_TILES = 4
REAL_WORKERS = 8


def _write(path: Path, array: np.ndarray, nodata=255) -> Path:
    profile = dict(driver="GTiff", height=array.shape[0], width=array.shape[1], count=1,
                   dtype="uint8", crs="EPSG:4326", nodata=nodata,
                   transform=from_origin(73.0, 31.0, 1e-4, 1e-4))
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array, 1)
    return path


def run(check):
    # ------------------------------------------------------------ R2-5, per-tile cost
    outcomes = [{"status": "ok", "seconds": s} for s in REAL_TILE_SECONDS]
    minutes, basis = ndvi_pipeline.per_tile_minutes(
        outcomes, REAL_WALL_MINUTES, REAL_TILES, REAL_WORKERS)
    check("real run: per-tile cost comes from farmdar's own durations",
          basis == "mean of farmdar's per-tile durations", basis)
    check("real run: 2.6 min/tile, not the 0.8 the old wall/tiles form gave",
          abs(minutes - 2.65) < 0.05 and abs(REAL_WALL_MINUTES / REAL_TILES - 0.825) < 0.05,
          f"{minutes:.2f} min/tile (old form would say {REAL_WALL_MINUTES / REAL_TILES:.2f})")

    cfg = PipelineConfig(crop="cane", year="2025", district_name="okara", aoi_path="x")
    check("the measured healthy figure sits under the shipped threshold",
          minutes < cfg.stac_slow_tile_warning_minutes,
          f"{minutes:.2f} < {cfg.stac_slow_tile_warning_minutes}")
    check("a 15 min/tile failure of the kind reported from the field would fire",
          ndvi_pipeline.per_tile_minutes([{"seconds": 900.0}], 15.0, 1, 8)[0]
          > cfg.stac_slow_tile_warning_minutes)

    # Fallback: farmdar gave no durations. Dividing by tiles would understate by the
    # worker count; dividing by batches does not.
    minutes, basis = ndvi_pipeline.per_tile_minutes(
        [{"status": "ok"} for _ in range(4)], REAL_WALL_MINUTES, 4, 8)
    check("no durations: falls back to wall-clock over batches, and says so",
          "batch(es) of 8 worker(s)" in basis, basis)
    check("no durations, 4 tiles on 8 workers: one batch, so min/tile == wall",
          abs(minutes - REAL_WALL_MINUTES) < 1e-9, f"{minutes}")
    minutes, _ = ndvi_pipeline.per_tile_minutes([{}] * 24, 12.0, 24, 8)
    check("no durations, 24 tiles on 8 workers: 3 batches, 4.0 min/tile",
          abs(minutes - 4.0) < 1e-9, f"{minutes}")
    minutes, basis = ndvi_pipeline.per_tile_minutes([], 5.0, 0, 0)
    check("degenerate input does not divide by zero",
          minutes == 5.0 and "1 batch(es) of 1 worker(s)" in basis, f"{minutes} / {basis}")

    # ------------------------------------------------------- R2-3, all-nodata refusals
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = PipelineConfig(crop="rice", year="2027", district_name="okara", aoi_path="x")

        all_nodata = _write(tmp / "all_nodata.tif", np.full((32, 32), 255, dtype=np.uint8))
        check.raises("NDVI stage refuses an all-nodata classification",
                     lambda: ndvi_pipeline._assert_classification_has_data(all_nodata, cfg),
                     RuntimeError, "nodata in every pixel")
        check.raises("and names it an acquisition failure, not an absent crop",
                     lambda: ndvi_pipeline._assert_classification_has_data(all_nodata, cfg),
                     RuntimeError, "acquisition failure")

        has_data = np.full((32, 32), 255, dtype=np.uint8)
        has_data[0, 0] = 4          # a single background pixel is still data
        one_pixel = _write(tmp / "one_pixel.tif", has_data)
        ndvi_pipeline._assert_classification_has_data(one_pixel, cfg)
        check("a raster with even one real pixel is accepted", True, "no raise")

        # Vectorising must refuse the same thing, and still write an empty layer for a
        # raster that is genuinely all background.
        aoi = tmp / "aoi.gpkg"
        gpd.GeoDataFrame({"id": [1]}, geometry=[box(72.99, 30.99, 73.01, 31.01)],
                         crs="EPSG:4326").to_file(aoi, driver="GPKG")
        check.raises("vectorising refuses an all-nodata raster",
                     lambda: postprocess.vectorize_process_and_export(
                         all_nodata, aoi, str(tmp / "outA"), "empty_test",
                         target_labels=[1], save_shp_zip=False),
                     RuntimeError, "nodata in every pixel")

        all_background = _write(tmp / "all_background.tif",
                                np.full((32, 32), 4, dtype=np.uint8))
        path = postprocess.vectorize_process_and_export(
            all_background, aoi, str(tmp / "outB"), "empty_test",
            target_labels=[1], save_shp_zip=False)
        check("a real raster with no crop still gets a path back, not None",
              path is not None, str(path))
        if path:
            gdf = gpd.read_file(path)
            check("and an empty layer with the full schema",
                  len(gdf) == 0 and list(gdf.columns) == ["predicted", "area_acres", "geometry"],
                  f"rows={len(gdf)} cols={list(gdf.columns)}")
