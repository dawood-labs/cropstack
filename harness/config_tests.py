"""Config-layer tests: things that must fail fast, before any imagery is acquired.

Covers the literal option strings in the test brief (some of which are not valid
values), the rice static-model gate, and the run-mode validator.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

REPO = Path("/home/jovyan/FAO/optimized_code_testing/cropstack")
sys.path.insert(0, str(REPO))
import os
os.chdir(REPO)

AOI = "/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_{crop}.shp"
KEY = "/home/jovyan/FAO/optimized_code_testing/gcs_data_downloader_ee_farmdar.json"

from config import build_pipeline_config  # noqa: E402

CASES = [
    # name, kwargs, expectation
    ("literal_brief_A4_static_source_stack",
     dict(crop="cane", year="2025", static_source="stack", gee_static_mode="manual"),
     "must raise: 'stack' is not a valid static_source"),
    ("literal_brief_gee_static_mode_manual",
     dict(crop="cane", year="2025", static_source="gee", gee_static_mode="manual"),
     "must raise: 'manual' is not a valid gee_static_mode"),
    ("literal_brief_A1_stac_mode_on_gee_source",
     dict(crop="cane", year="2025", static_source="gee", stac_static_mode="auto"),
     "stac_static_mode is ignored when static_source='gee' -- expect PASS (silently ignored)"),
    ("literal_brief_A3_gee_mode_on_stac_source",
     dict(crop="cane", year="2025", static_source="stac", gee_static_mode="api_auto"),
     "gee_static_mode is ignored when static_source='stac' -- expect PASS (silently ignored)"),
    ("rice_default_no_static",
     dict(crop="rice", year="2025"),
     "must pass with run_static_model=False"),
    ("rice_force_static_true",
     dict(crop="rice", year="2025", run_static_model=True),
     "must raise a clear error naming run_static_model"),
    ("stac_static_manual_without_dates",
     dict(crop="cane", year="2025", static_source="stac", stac_static_mode="manual"),
     "must raise: manual mode needs stac_static_dates"),
    ("gee_api_manual_without_dates",
     dict(crop="cane", year="2025", static_source="gee", gee_static_mode="api_manual"),
     "must raise: api_manual needs dates"),
    ("gee_manual_gcs_link_without_uri",
     dict(crop="cane", year="2025", static_source="gee", gee_static_mode="manual_gcs_link"),
     "must raise: needs gee_static_gcs_uri"),
    ("bad_run_mode",
     dict(crop="cane", year="2025", run_mode="rewind"),
     "must raise: invalid run mode"),
    ("unknown_config_field",
     dict(crop="cane", year="2025", nonexistent_option=1),
     "must raise TypeError for unknown field"),
    ("bad_aoi_path",
     dict(crop="cane", year="2025", _aoi_override="/nonexistent/path/nope.shp"),
     "must raise FileNotFoundError at config build time"),
    ("cane_2016_landsat_switch",
     dict(crop="cane", year="2016", ndvi_source="gee", static_source="gee"),
     "2016 < cutover 2018 -> LANDSAT, 30 m, sieve min pixels 1"),
]


def run_case(name, kwargs, expectation):
    kwargs = dict(kwargs)
    crop = kwargs.pop("crop")
    year = kwargs.pop("year")
    aoi = kwargs.pop("_aoi_override", AOI.format(crop=crop))
    record = {"name": name, "expectation": expectation,
              "kwargs": {"crop": crop, "year": year, "aoi_path": aoi, **kwargs}}
    try:
        cfg = build_pipeline_config(
            crop, year, "okara", aoi_path=aoi, gee_service_account_key=KEY, **kwargs)
        cfg.validate()
        record["result"] = "PASSED_VALIDATION"
        record["run_static_model"] = cfg.run_static_model
        record["details"] = {
            "static_source": cfg.static_source,
            "stac_static_mode": cfg.stac_static_mode,
            "gee_static_mode": cfg.gee_static_mode,
            "sensor_mode": cfg.gee_sensor_mode,
            "ndvi_resolution_m": cfg.ndvi_resolution_m,
            "static_resolution_m": cfg.static_resolution_m,
            "ndvi_sieve_min_pixels": cfg.ndvi_sieve_min_pixels,
            "static_sieve_min_pixels": cfg.static_sieve_min_pixels,
            "composite_step_days": cfg.composite_step_days,
            "needs_gee_api": cfg.needs_gee_api,
        }
    except Exception as exc:
        record["result"] = "RAISED"
        record["error_type"] = type(exc).__name__
        record["error"] = str(exc)
        record["traceback"] = traceback.format_exc()
    return record


def main():
    results = [run_case(*case) for case in CASES]
    out = Path("/home/jovyan/FAO/optimized_code_testing/metrics/config_tests.json")
    out.write_text(json.dumps(results, indent=2))
    for record in results:
        print(f"\n### {record['name']}")
        print(f"    expect : {record['expectation']}")
        print(f"    result : {record['result']}")
        if record["result"] == "RAISED":
            print(f"    {record['error_type']}: {record['error']}")
        else:
            print(f"    run_static_model={record.get('run_static_model')} "
                  f"details={json.dumps(record.get('details', {}))}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
