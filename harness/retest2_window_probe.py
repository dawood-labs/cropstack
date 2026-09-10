"""RETEST_2: exercise select_dates_by_priority directly.

Scores every configured window (metadata + coarse SCL only, no imagery download) and
reports what the selector chooses under different `static_window_preference_margin_pct`
values, plus the window-expansion fallback. Cheap enough to run many variants.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/jovyan/FAO/optimized_code_testing/cropstack")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                    stream=sys.stdout, force=True)

from config import build_pipeline_config          # noqa: E402
from static_pipeline import select_dates_by_priority  # noqa: E402

AOI = "/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_spr_maize.shp"
KEY = "/home/jovyan/FAO/optimized_code_testing/gcs_data_downloader_ee_farmdar.json"


def probe(label, crop, year, aoi=AOI, **overrides):
    print("\n" + "=" * 78)
    print(f"### {label}")
    print("=" * 78, flush=True)
    started = time.time()
    record = {"label": label, "crop": crop, "year": year, "overrides": overrides}
    try:
        cfg = build_pipeline_config(crop, year, "okara", aoi_path=aoi,
                                    gee_service_account_key=KEY,
                                    ndvi_source="stac", static_source="stac",
                                    stac_static_mode="auto", **overrides)
        record["configured_windows"] = [list(w) for w in cfg.resolved_static_windows()]
        dates, selection, description = select_dates_by_priority(cfg)
        record.update(status="ok", dates=dates, description=description,
                      coverage_pct=selection.get("coverage_pct"),
                      window_scores=selection.get("window_scores"))
    except BaseException as exc:
        record.update(status="raised", error=f"{type(exc).__name__}: {exc}")
        print(f"RAISED {type(exc).__name__}: {exc}", flush=True)
    record["seconds"] = round(time.time() - started, 1)
    print("\n--- probe result ---")
    print(json.dumps(record, indent=2, default=str), flush=True)
    return record


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    out = []
    if which in ("all", "margin"):
        out.append(probe("spr_maize 2025 margin=5 (DEFAULT)", "spr_maize", "2025"))
        out.append(probe("spr_maize 2025 margin=0", "spr_maize", "2025",
                         static_window_preference_margin_pct=0))
        out.append(probe("spr_maize 2025 margin=100", "spr_maize", "2025",
                         static_window_preference_margin_pct=100))
        out.append(probe("spr_maize 2025 start_at=2", "spr_maize", "2025",
                         static_window_start_at=2))
    if which in ("all", "expansion"):
        # 2014: Sentinel-2A launched 2015-06, so no configured window can have imagery.
        out.append(probe("spr_maize 2014 (no imagery) expansion=5d x3 DEFAULT",
                         "spr_maize", "2014"))
        out.append(probe("spr_maize 2014 expansion DISABLED (=0)", "spr_maize", "2014",
                         static_window_expansion_days=0))
        out.append(probe("spr_maize 2014 expansion=5d x1", "spr_maize", "2014",
                         static_window_max_expansions=1))
    Path("/home/jovyan/FAO/optimized_code_testing/metrics").mkdir(exist_ok=True)
    Path(f"/home/jovyan/FAO/optimized_code_testing/metrics/retest2_window_probe_{which}.json").write_text(
        json.dumps(out, indent=2, default=str))
    print(f"\nwrote metrics/retest2_window_probe_{which}.json")
