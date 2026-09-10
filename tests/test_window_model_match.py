"""The inference window must select as many composites as the model was trained on.

A Random Forest reads its features positionally, so a window that is off by one
composite still predicts, and still writes a map -- every date just gets compared
against the wrong point in the crop calendar. The cotton 2025 model was trained on
2025-04-01..2025-12-29 (35 composites); the old notebook pipeline asked for
2025-04-02.. which dropped the first band and handed it 04-09..12-31 instead. On a
Layyah test AOI that shift raised mapped cotton from 4.63% to 7.57% of pixels, and
nothing in the run said a word. Hence the guard, and hence this test.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import joblib
import numpy as np
import rasterio
from rasterio.transform import from_origin
from sklearn.ensemble import RandomForestClassifier

import ndvi_pipeline
from config import PipelineConfig

# The real cotton 2025 composite dates: 8-day steps, named by the window's end date.
COTTON_DATES = [
    "2025-04-01", "2025-04-09", "2025-04-17", "2025-04-25", "2025-05-03", "2025-05-11",
    "2025-05-19", "2025-05-27", "2025-06-04", "2025-06-12", "2025-06-20", "2025-06-28",
    "2025-07-06", "2025-07-14", "2025-07-22", "2025-07-30", "2025-08-07", "2025-08-15",
    "2025-08-23", "2025-08-31", "2025-09-08", "2025-09-16", "2025-09-24", "2025-10-02",
    "2025-10-10", "2025-10-18", "2025-10-26", "2025-11-03", "2025-11-11", "2025-11-19",
    "2025-11-27", "2025-12-05", "2025-12-13", "2025-12-21", "2025-12-29", "2025-12-31",
]


def _write_tile(path: Path, dates) -> Path:
    """A red/nir stack named the way farmdar.sentinel names one."""
    count = 2 * len(dates)
    profile = dict(driver="GTiff", height=4, width=4, count=count, dtype="uint16",
                   crs="EPSG:4326", transform=from_origin(71.0, 31.0, 1e-4, 1e-4))
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.ones((count, 4, 4), dtype="uint16"))
        for i, date in enumerate(dates):
            stamp = date.replace("-", "_")
            dst.set_band_description(2 * i + 1, f"red_{stamp}")
            dst.set_band_description(2 * i + 2, f"nir_{stamp}")
    return path


def _write_model(path: Path, n_features: int) -> Path:
    model = RandomForestClassifier(n_estimators=2, random_state=0)
    rng = np.random.default_rng(0)
    model.fit(rng.random((8, n_features)), [1, 4] * 4)
    joblib.dump(model, path)
    return path


class Opaque:
    """A model object that does not report how many features it was fitted on."""


def _cfg(start: str, end: str, tmp: Path, training_dates=None) -> PipelineConfig:
    return PipelineConfig(
        crop="cotton", year="2025", district_name="test",
        aoi_path=str(tmp / "aoi.gpkg"),
        run_static_model=False,
        ndvi_inference_start=start, ndvi_inference_end=end,
        ndvi_training_dates=training_dates,
    )


TRAINED = COTTON_DATES[:35]          # 2025-04-01 .. 2025-12-29


def run(check):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        tile = _write_tile(tmp / "tile.tif", COTTON_DATES)
        model = _write_model(tmp / "rf.joblib", 35)

        # --------------------------------------------- dates pinned: the strong check
        ndvi_pipeline._assert_inference_window_matches_model(
            tile, _cfg("2025-04-01", "2025-12-29", tmp, TRAINED), str(model))
        check("the trained window passes", True, "35 composites, 2025-04-01..2025-12-29")

        # The off-by-one that shipped once. It selects 35 composites too, so only a
        # date-level check can see it -- this is the case the band count cannot catch.
        check.raises(
            "the shifted window is refused even though the count matches",
            lambda: ndvi_pipeline._assert_inference_window_matches_model(
                tile, _cfg("2025-04-02", "2025-12-31", tmp, TRAINED), str(model)),
            ValueError, contains="2025-04-01",
        )

        try:
            ndvi_pipeline._assert_inference_window_matches_model(
                tile, _cfg("2025-04-02", "2025-12-31", tmp, TRAINED), str(model))
            message = ""
        except ValueError as exc:
            message = str(exc)
        check("the message names the date that went missing",
              "Missing" in message and "2025-04-01" in message, message[:110])
        check("and the date that arrived instead",
              "Unexpected" in message and "2025-12-31" in message, message[:110])

        # ------------------------------------- no dates pinned: the band-count fallback
        check.raises(
            "a truncated window is refused on width alone",
            lambda: ndvi_pipeline._assert_inference_window_matches_model(
                tile, _cfg("2025-04-01", "2025-10-02", tmp), str(model)),
            ValueError, contains="trained on 35",
        )

        # Honest about its own limit: with no dates configured, a pure shift that keeps
        # the count gets through. That is why cotton pins its dates.
        ndvi_pipeline._assert_inference_window_matches_model(
            tile, _cfg("2025-04-02", "2025-12-31", tmp), str(model))
        check("width alone cannot see a shift that keeps the count", True,
              "the reason ndvi_training_dates exists")

        # -------------------------------------------- a model that cannot say, is let by
        joblib.dump(Opaque(), tmp / "opaque.joblib")
        ndvi_pipeline._assert_inference_window_matches_model(
            tile, _cfg("2025-04-02", "2025-12-31", tmp), str(tmp / "opaque.joblib"))
        check("a model without n_features_in_ is not blocked", True,
              "nothing to compare against")
