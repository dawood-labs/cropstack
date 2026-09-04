"""R2-2 on real data: staging tiles must not survive a change of date -- and must
survive when the date has not changed.

The original defect: `static_staging/` holds `static_10m_tile_0001.tif`, no date in the
name, and was cleared only on success. A second run in the same folder asking for 9 May
mosaicked the 29 April tiles and wrote them out under the 9 May name. Byte-identical
products, every label wrong.

The fix must not swing the other way. If a same-date resume discarded its tiles it would
re-download ~2.5 GB every time, which is a new problem rather than a fix, so that is
checked here too -- in manual mode and in the auto/priority-window mode a real district
run actually uses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
REPO = ROOT / "cropstack"
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                    stream=sys.stdout, force=True)

AOI = str(ROOT / "test_aois_small/okara_test_data_spr_maize.shp")
KEY = str(ROOT / "gcs_data_downloader_ee_farmdar.json")
SEED_NDVI = ROOT / "runs_retest/P4_sprmaize_2025/1_ndvi_run_1"
RUN = ROOT / "runs_retest/V2_staging"


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()[:12]


def staging_state(out: Path) -> dict:
    staging = out / "2_static_run_1" / "static_staging"
    tiles = sorted(staging.glob("*.tif")) if staging.exists() else []
    record = staging / ".staging.json"
    return {
        "tile_count": len(tiles),
        "tile_md5": {t.name: md5(t) for t in tiles},
        "recorded_dates": (json.loads(record.read_text()).get("dates")
                           if record.exists() else None),
    }


def run_once(out: Path, label: str, **overrides) -> dict:
    from config import build_pipeline_config
    import pipeline

    print("\n" + "=" * 78)
    print(f"### {label}")
    print("=" * 78, flush=True)
    before = staging_state(out)
    started = time.time()
    cfg = build_pipeline_config(
        "spr_maize", "2025", "okara", aoi_path=AOI, gee_service_account_key=KEY,
        output_dir=str(out), ndvi_source="stac", static_source="stac",
        run_mode="resume", delete_raw_static_tiles=False, **overrides)
    outcome = pipeline.run_pipeline(cfg)
    after = staging_state(out)
    static_raster = Path(outcome["sieved_static_raster"])
    check = outcome.get("result_check") or {}
    record = {
        "label": label, "seconds": round(time.time() - started, 1),
        "date_folder": static_raster.parent.name,
        "product_md5": md5(static_raster),
        "acres": check.get("crop_acres"), "features": check.get("feature_count"),
        "staging_before": before, "staging_after": after,
        "tiles_changed": before["tile_md5"] != after["tile_md5"],
    }
    print("\n--- run result ---")
    print(json.dumps(record, indent=2, default=str), flush=True)
    return record


def main() -> int:
    if RUN.exists():
        shutil.rmtree(RUN)
    RUN.mkdir(parents=True)
    shutil.copytree(SEED_NDVI, RUN / "1_ndvi_run_1")

    a = run_once(RUN, "A manual 2025-04-29 (populates staging)",
                 stac_static_mode="manual", stac_static_dates=["2025-04-29"])
    b = run_once(RUN, "B manual 2025-05-09 in the SAME folder (must discard A's tiles)",
                 stac_static_mode="manual", stac_static_dates=["2025-05-09"])
    c = run_once(RUN, "C manual 2025-05-09 again (must REUSE B's tiles)",
                 stac_static_mode="manual", stac_static_dates=["2025-05-09"])

    auto_run = ROOT / "runs_retest/V2_staging_auto"
    if auto_run.exists():
        shutil.rmtree(auto_run)
    auto_run.mkdir(parents=True)
    shutil.copytree(SEED_NDVI, auto_run / "1_ndvi_run_1")
    d = run_once(auto_run, "D auto/priority-window run", stac_static_mode="auto")
    e = run_once(auto_run, "E auto again, same folder (must REUSE D's tiles)",
                 stac_static_mode="auto")

    results = [a, b, c, d, e]
    (ROOT / "metrics" / "verify_r2_2.json").write_text(json.dumps(results, indent=2, default=str))
    print("\nwrote metrics/verify_r2_2.json")

    ok = True

    def assert_(name, condition, detail=""):
        nonlocal ok
        ok = ok and bool(condition)
        print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))

    print("\n=== verdict ===")
    assert_("A staged tiles are recorded against 2025-04-29",
            a["staging_after"]["recorded_dates"] == ["2025-04-29"],
            str(a["staging_after"]["recorded_dates"]))
    assert_("B discarded A's tiles rather than reusing them",
            b["tiles_changed"], "staging tile md5s changed" if b["tiles_changed"] else "REUSED STALE TILES")
    assert_("B's product differs from A's -- the 9 May answer is not the 29 April one",
            b["product_md5"] != a["product_md5"],
            f"A={a['product_md5']} ({a['acres']} ac) B={b['product_md5']} ({b['acres']} ac)")
    assert_("B is recorded against 2025-05-09",
            b["staging_after"]["recorded_dates"] == ["2025-05-09"],
            str(b["staging_after"]["recorded_dates"]))
    assert_("C reused B's tiles (no re-download on a same-date resume)",
            not c["tiles_changed"], "tiles identical" if not c["tiles_changed"] else "RE-DOWNLOADED")
    assert_("C is byte-identical to B",
            c["product_md5"] == b["product_md5"], f"{c['product_md5']} vs {b['product_md5']}")
    assert_("E reused D's tiles in auto/priority-window mode",
            not e["tiles_changed"], "tiles identical" if not e["tiles_changed"] else "RE-DOWNLOADED")
    assert_("E is byte-identical to D",
            e["product_md5"] == d["product_md5"], f"{e['product_md5']} vs {d['product_md5']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
