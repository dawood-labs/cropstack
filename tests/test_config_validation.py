"""`PipelineConfig.validate()` and `aoi_io.resolve_aoi` catch bad input before any
acquisition, rather than after.

Regression tests for defects found and fixed during a 2026-09-16 audit, all reproduced
against the real pipeline before being fixed (see TEST_REPORT_2026-09-16.md):

* RT-1 -- the STAC path (the default) had no AOI geometry validation at all. An empty AOI
  crashed deep inside farmdar's tile-grid math; a points-only AOI (no polygon at all)
  passed validate() and started real Sentinel-2 acquisition.
* N-1  -- gee_static_mode='api_manual' accepted both gee_static_single_date and a
  top/bottom pair at once; the pair silently won and the single date was dropped with no
  warning. Extended after a follow-up question about the remaining edge cases into
  `resolved_gee_manual_layering()` plus the `gee_static_manual_layering` toggle: every
  combination of single/top/bottom now either resolves unambiguously, is rejected before
  any acquisition with a message naming the fix, or is decided explicitly by the toggle.
* N-9  -- an unknown region (e.g. a typo) was not caught by validate() or --print-config;
  it only raised once the static stage began, after the NDVI stage had already run.
* N-5/N-6 -- region set for a crop with no regional windows, and stac_static_dates set in
  'auto' mode for a crop with priority windows, are both legitimate no-ops (not errors --
  see build_jobs' shared_overrides) but previously gave no indication that the field had
  no effect.

Offline: no farmdar, no network, no credentials beyond a placeholder key file for the one
case that needs `needs_gcs=True` to reach the check being tested.
"""
from __future__ import annotations

import json
import logging
import tempfile
import warnings
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, Polygon, box

import aoi_io
from config import build_pipeline_config


def _write_aoi(path: Path, geometries) -> Path:
    gpd.GeoDataFrame({"geometry": geometries}, crs="EPSG:4326").to_file(path, driver="GPKG")
    return path


def _fake_key(path: Path) -> Path:
    path.write_text("{}")
    return path


def _last_warning(fn) -> str:
    """Runs `fn()` and returns the last logging.warning message it emitted, or ''."""
    messages = []

    class _Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = _Capture()
    logging.getLogger("config").addHandler(handler)
    try:
        fn()
    finally:
        logging.getLogger("config").removeHandler(handler)
    return messages[-1] if messages else ""


def run(check):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        real_aoi = _write_aoi(tmp / "real.gpkg", [box(73.37, 31.59, 73.44, 31.65)])
        empty_aoi = _write_aoi(tmp / "empty.gpkg", [])
        points_aoi = _write_aoi(tmp / "points.gpkg", [Point(73.4, 31.6), Point(73.41, 31.61)])
        bowtie = Polygon([(73.37, 31.60), (73.44, 31.65), (73.37, 31.65), (73.44, 31.60), (73.37, 31.60)])
        bowtie_aoi = _write_aoi(tmp / "bowtie.gpkg", [bowtie])
        mixed_aoi = _write_aoi(tmp / "mixed.gpkg", [box(73.37, 31.59, 73.44, 31.65), bowtie])
        key_path = _fake_key(tmp / "key.json")

        # ------------------------------------------------------------------- RT-1
        resolved = aoi_io.resolve_aoi(str(real_aoi))
        check("RT-1: a valid AOI resolves unchanged", str(resolved) == str(real_aoi))

        check.raises("RT-1: an empty AOI is rejected before farmdar ever sees it",
                     lambda: aoi_io.resolve_aoi(str(empty_aoi)),
                     ValueError, "no features")

        check.raises("RT-1: a points-only AOI (no polygon) is rejected, not acquired",
                     lambda: aoi_io.resolve_aoi(str(points_aoi)),
                     ValueError, "no polygon geometry")

        cleaned = aoi_io.resolve_aoi(str(bowtie_aoi))
        cleaned_gdf = gpd.read_file(cleaned)
        check("RT-1: a self-intersecting polygon is repaired, not silently accepted broken",
              str(cleaned) != str(bowtie_aoi) and cleaned_gdf.geometry.is_valid.all(),
              f"resolved={cleaned}")

        mixed_cleaned = gpd.read_file(aoi_io.resolve_aoi(str(mixed_aoi)))
        check("RT-1: a mix of valid + invalid geometry keeps both, repairing only the bad one",
              len(mixed_cleaned) == 2 and mixed_cleaned.geometry.is_valid.all(),
              f"rows={len(mixed_cleaned)}")

        # -------------------------------------------------------------- N-3 (UTM zone)
        check("N-3: Faisalabad (73.4E) resolves to UTM zone 43N, not a fixed 42N",
              aoi_io.local_utm_epsg(73.41, 31.62) == 32643,
              f"{aoi_io.local_utm_epsg(73.41, 31.62)}")
        check("N-3: a point west of the 42N/43N boundary still resolves to 42N",
              aoi_io.local_utm_epsg(69.0, 31.0) == 32642,
              f"{aoi_io.local_utm_epsg(69.0, 31.0)}")
        check("N-3: southern hemisphere resolves to the 327xx series, not 326xx",
              aoi_io.local_utm_epsg(30.0, -20.0) == 32736,
              f"{aoi_io.local_utm_epsg(30.0, -20.0)}")

        # --------------------------------------------------------------------- N-9
        cfg = build_pipeline_config("wheat", "2025", "d", aoi_path=str(real_aoi), region="balochistan")
        check.raises("N-9: an unknown region is caught by validate(), before acquisition",
                     cfg.validate, ValueError, "No static windows defined for region")

        cfg_ok = build_pipeline_config("wheat", "2025", "d", aoi_path=str(real_aoi), region="punjab")
        try:
            cfg_ok.validate()
            check("N-9: a valid region still passes validate()", True)
        except Exception as exc:  # noqa: BLE001
            check("N-9: a valid region still passes validate()", False, f"{type(exc).__name__}: {exc}")

        cfg_none = build_pipeline_config("cane", "2025", "d", aoi_path=str(real_aoi))
        try:
            cfg_none.validate()
            check("N-9 regression: no region at all still passes validate()", True)
        except Exception as exc:  # noqa: BLE001
            check("N-9 regression: no region at all still passes validate()", False, f"{exc}")

        # --------------------------------------------------------------------- N-1
        def _gee_manual_cfg(**overrides):
            return build_pipeline_config(
                "cane", "2025", "d", aoi_path=str(real_aoi),
                static_source="gee", gee_static_mode="api_manual",
                gee_service_account_key=str(key_path), **overrides,
            )

        check("N-1.1: single date alone resolves to 'single'",
              _gee_manual_cfg(gee_static_single_date="2025-11-10")
              .resolved_gee_manual_layering() == "single")

        check("N-1.2: a full top/bottom pair alone resolves to 'mosaic'",
              _gee_manual_cfg(gee_static_top_date="2025-11-10", gee_static_bottom_date="2025-10-16")
              .resolved_gee_manual_layering() == "mosaic")

        check.raises(
            "N-1.3: single_date + a full top/bottom pair, no toggle, is a contradiction",
            _gee_manual_cfg(gee_static_single_date="2025-11-10", gee_static_top_date="2025-11-10",
                            gee_static_bottom_date="2025-10-16").resolved_gee_manual_layering,
            ValueError, "got both gee_static_single_date")

        check("N-1.4: all three set, toggle='single' -> single wins",
              _gee_manual_cfg(gee_static_single_date="2025-11-10", gee_static_top_date="2025-11-10",
                              gee_static_bottom_date="2025-10-16", gee_static_manual_layering="single")
              .resolved_gee_manual_layering() == "single")

        check("N-1.5: all three set, toggle='mosaic' -> mosaic wins",
              _gee_manual_cfg(gee_static_single_date="2025-11-10", gee_static_top_date="2025-11-10",
                              gee_static_bottom_date="2025-10-16", gee_static_manual_layering="mosaic")
              .resolved_gee_manual_layering() == "mosaic")

        check.raises(
            "N-1.6: single_date + a stray top_date (no bottom), no toggle, is caught before acquisition",
            _gee_manual_cfg(gee_static_single_date="2025-11-10", gee_static_top_date="2025-11-10")
            .resolved_gee_manual_layering,
            ValueError, "is set together with only one of")

        check.raises(
            "N-1.7: the same stray-top-date case with toggle='mosaic' still checks both dates",
            _gee_manual_cfg(gee_static_single_date="2025-11-10", gee_static_top_date="2025-11-10",
                            gee_static_manual_layering="mosaic").resolved_gee_manual_layering,
            ValueError, "requires both gee_static_top_date")

        check("N-1.8: the same stray-top-date case with toggle='single' uses single, ignores the stray date",
              _gee_manual_cfg(gee_static_single_date="2025-11-10", gee_static_top_date="2025-11-10",
                              gee_static_manual_layering="single")
              .resolved_gee_manual_layering() == "single")

        check.raises("N-1.9: only top_date, no bottom, no single, is caught",
                     _gee_manual_cfg(gee_static_top_date="2025-11-10").resolved_gee_manual_layering,
                     ValueError, "only one of")

        check.raises("N-1.10: an invalid toggle value is rejected",
                     _gee_manual_cfg(gee_static_single_date="2025-11-10",
                                     gee_static_manual_layering="bogus").resolved_gee_manual_layering,
                     ValueError, "must be 'single' or 'mosaic'")

        check.raises("N-1.11: nothing set at all is caught, same message as before this fix",
                     _gee_manual_cfg().resolved_gee_manual_layering,
                     ValueError, "requires gee_static_single_date")

        # validate() itself must exercise the same path, not just resolved_gee_manual_layering()
        check.raises("N-1.12: validate() itself raises on the ambiguous case, before any acquisition",
                     _gee_manual_cfg(gee_static_single_date="2025-11-10", gee_static_top_date="2025-11-10",
                                     gee_static_bottom_date="2025-10-16").validate,
                     ValueError, "got both gee_static_single_date")

        # ----------------------------------------------------------------- N-5 / N-6
        cfg_region_noop = build_pipeline_config("cane", "2025", "d", aoi_path=str(real_aoi),
                                                 region="totally_bogus_region")
        message = _last_warning(cfg_region_noop.validate)
        check("N-5: region set for a crop with no regional windows warns, doesn't error",
              "has no effect for this crop" in message, message)

        cfg_dates_noop = build_pipeline_config("cane", "2025", "d", aoi_path=str(real_aoi),
                                                stac_static_mode="auto",
                                                stac_static_dates=["2025-01-01"])
        message = _last_warning(cfg_dates_noop.validate)
        check("N-6: stac_static_dates set in auto mode (priority windows win) warns",
              "has no effect" in message, message)

        cfg_dates_used = build_pipeline_config("cane", "2025", "d", aoi_path=str(real_aoi),
                                                stac_static_mode="manual",
                                                stac_static_dates=["2025-01-01"])
        message = _last_warning(cfg_dates_used.validate)
        check("N-6 regression: stac_static_dates in manual mode (where it's used) warns of nothing",
              message == "", f"unexpected warning: {message!r}")
