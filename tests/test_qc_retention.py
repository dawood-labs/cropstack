"""`qc.assess_result` reports retention; it does not judge it.

These lock in the removal of the fitted 15% / 99.5% band. The band was derived from six
observations on one AOI and shipped as a verdict; what a static model *should* keep is a
property of the crop, the district and the model, and nothing at run time knows it.

Rasters are written to a temp directory rather than mocked, so the block-wise pixel
counting in `_count_labelled_pixels` is exercised for real.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

import qc
from config import PipelineConfig

SIZE = 64
NDVI_CROP_CLASSES = (1,)
CROP_LABEL, BACKGROUND_LABEL = 1, 4


def _write_raster(path: Path, array: np.ndarray) -> Path:
    profile = dict(driver="GTiff", height=array.shape[0], width=array.shape[1], count=1,
                   dtype="uint8", crs="EPSG:4326", nodata=255,
                   transform=from_origin(73.0, 31.0, 1e-4, 1e-4))
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array, 1)
    return path


def _scene(tmp: Path, ndvi_crop_px: int, static_crop_px: int):
    """An NDVI map with `ndvi_crop_px` crop pixels and a static map with a subset kept."""
    ndvi = np.full((SIZE, SIZE), BACKGROUND_LABEL, dtype=np.uint8)
    ndvi.reshape(-1)[:ndvi_crop_px] = CROP_LABEL
    static = np.full((SIZE, SIZE), BACKGROUND_LABEL, dtype=np.uint8)
    static.reshape(-1)[:static_crop_px] = CROP_LABEL
    return (_write_raster(tmp / f"ndvi_{ndvi_crop_px}.tif", ndvi),
            _write_raster(tmp / f"static_{ndvi_crop_px}_{static_crop_px}.tif", static))


def _aoi(tmp: Path) -> Path:
    path = tmp / "aoi.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(73.0, 30.99, 73.0064, 31.0)],
                     crs="EPSG:4326").to_file(path, driver="GPKG")
    return path


def _vectors(tmp: Path, acres: float) -> Path:
    path = tmp / "result.gpkg"
    gpd.GeoDataFrame(
        {"predicted": pd.Series([1], dtype="int64"), "area_acres": pd.Series([acres], dtype="float64")},
        geometry=[box(73.0, 30.995, 73.003, 31.0)], crs="EPSG:4326",
    ).to_file(path, driver="GPKG")
    return path


def _assess(tmp: Path, ndvi_px: int, static_px: int, **overrides) -> dict:
    ndvi_raster, static_raster = _scene(tmp, ndvi_px, static_px)
    kwargs = dict(vector_path=_vectors(tmp, 100.0), aoi_path=_aoi(tmp),
                  ndvi_raster=ndvi_raster, ndvi_crop_classes=NDVI_CROP_CLASSES,
                  static_raster=static_raster, static_crop_label=CROP_LABEL)
    kwargs.update(overrides)
    return qc.assess_result(**kwargs)


def run(check):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ---------------------------------------------- the defaults judge nothing
        cfg = PipelineConfig(crop="cane", year="2025", district_name="okara", aoi_path="x")
        check("config: qc_min_static_retention_pct defaults to None",
              cfg.qc_min_static_retention_pct is None, repr(cfg.qc_min_static_retention_pct))
        check("config: qc_max_static_retention_pct defaults to None",
              cfg.qc_max_static_retention_pct is None, repr(cfg.qc_max_static_retention_pct))
        check("config: crop-share bounds still default to None",
              cfg.qc_max_crop_share_pct is None and cfg.qc_min_crop_share_pct is None)
        check("config: degenerate tolerance defaults to exact (0.0)",
              cfg.qc_degenerate_retention_tolerance_pct == 0.0,
              f"{cfg.qc_degenerate_retention_tolerance_pct}")

        # A retention that once tripped the 15% floor must now pass silently. 4.8% is the
        # real spr_maize window-3 figure that the fitted floor flagged.
        report = _assess(tmp, 1000, 48)
        check("retention 4.8% raises no warning under the new defaults",
              report["warnings"] == [], f"warnings={report['warnings']}")
        check("retention 4.8% is still reported",
              report["static_retention_pct"] == 4.8, f"{report['static_retention_pct']}")

        for ndvi_px, static_px, expected in [(1000, 205, 20.5), (1000, 424, 42.4), (1000, 900, 90.0)]:
            report = _assess(tmp, ndvi_px, static_px)
            check(f"retention {expected}% reported and unjudged",
                  report["static_retention_pct"] == expected and report["warnings"] == [],
                  f"pct={report['static_retention_pct']} warnings={len(report['warnings'])}")

        # ------------------------------------------------- the two degenerate cases
        report = _assess(tmp, 1000, 0)
        check("retention 0% warns (static kept nothing)",
              len(report["warnings"]) == 1 and "effectively none" in report["warnings"][0],
              f"{report['warnings']}")
        check("retention 0% is reported as 0.0, not omitted",
              report["static_retention_pct"] == 0.0, f"{report['static_retention_pct']}")

        report = _assess(tmp, 1000, 1000)
        check("retention 100% warns (static removed nothing)",
              len(report["warnings"]) == 1 and "effectively all" in report["warnings"][0],
              f"{report['warnings']}")

        # The default is exact, and the cost of that is explicit: a near-collapse is
        # reported and not warned about. This is the case to revisit if one ever shows up
        # in the field.
        check("DEFAULT is exact: 0.3% retention -- a near-collapse -- does NOT warn",
              _assess(tmp, 1000, 3)["warnings"] == [],
              "reported as 0.3, no warning")
        check("DEFAULT is exact: 0.3% is still reported",
              _assess(tmp, 1000, 3)["static_retention_pct"] == 0.3)
        check("DEFAULT is exact: 99.7% does NOT warn",
              _assess(tmp, 1000, 997)["warnings"] == [])
        check("retention 2% does not warn",
              _assess(tmp, 1000, 20)["warnings"] == [])
        check("retention 98% does not warn",
              _assess(tmp, 1000, 980)["warnings"] == [])

        # A tolerance can still be set by an operator who has a reason to.
        check("tolerance=1.0: 0.5% counts as 'none'",
              len(_assess(tmp, 1000, 5, degenerate_retention_tolerance_pct=1.0)["warnings"]) == 1)
        check("tolerance=1.0: 99.6% counts as 'all'",
              len(_assess(tmp, 1000, 996, degenerate_retention_tolerance_pct=1.0)["warnings"]) == 1)

        # ------------------------------------------- operator-set bounds still work
        report = _assess(tmp, 1000, 205, min_static_retention_pct=30.0)
        check("an operator-set floor still warns when crossed",
              len(report["warnings"]) == 1 and "floor set for this run" in report["warnings"][0],
              f"{report['warnings']}")
        report = _assess(tmp, 1000, 500, max_static_retention_pct=40.0)
        check("an operator-set ceiling still warns when crossed",
              len(report["warnings"]) == 1 and "ceiling set for this run" in report["warnings"][0],
              f"{report['warnings']}")

        # --------------------------------------------------- always reported at all
        report = _assess(tmp, 0, 0)
        check("retention is reported as null when the NDVI stage found no crop",
              "static_retention_pct" in report and report["static_retention_pct"] is None,
              f"{report.get('static_retention_pct', '<missing>')}")
        check("no degenerate warning fires when retention is undefined",
              report["warnings"] == [], f"{report['warnings']}")

        report_path = tmp / "sub" / "result_check.json"
        report = _assess(tmp, 1000, 205, report_path=report_path)
        on_disk = json.loads(report_path.read_text())
        check("static_retention_pct is written to result_check.json",
              on_disk.get("static_retention_pct") == 20.5, f"{on_disk.get('static_retention_pct')}")
        for field in ("aoi_acres", "crop_acres", "crop_share_of_aoi_pct", "static_retention_pct"):
            check(f"result_check.json carries {field}", field in on_disk, str(on_disk.get(field)))
