"""R2-3 on real data: a year outside the archive and a district with no crop must not
produce the same output.

Before the fix, 2027 returned tiles carrying no pixels, every stage succeeded on empty
arrays, and an NDVI-only crop delivered a clean 0-acre GPKG with no warning -- identical
in shape to the honest answer for ground where the crop simply is not grown.

Three real runs, no patching:
  1  spr_maize 2027   -- static path, year outside the archive
  2  rice 2027        -- NDVI-only path, the one that used to ship a clean zero
  3  cane on a 1,324-acre clipped sub-AOI of Okara where the pipeline's own cane
     classification returns zero crop pixels -- real ground, real zero
"""
from __future__ import annotations

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

KEY = str(ROOT / "gcs_data_downloader_ee_farmdar.json")
AOIS = ROOT / "test_aois_small"


def case(label: str, name: str, crop: str, year: str, aoi: str, **overrides) -> dict:
    from config import build_pipeline_config
    import pipeline

    print("\n" + "=" * 78)
    print(f"### {label}")
    print("=" * 78, flush=True)
    out = ROOT / "runs_retest" / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    record: dict = {"case": label, "run": name, "crop": crop, "year": year}
    started = time.time()
    try:
        cfg = build_pipeline_config(crop, year, "okara", aoi_path=aoi,
                                    gee_service_account_key=KEY, output_dir=str(out),
                                    ndvi_source="stac", static_source="stac",
                                    stac_static_mode="auto", run_mode="resume", **overrides)
        outcome = pipeline.run_pipeline(cfg)
        record["status"] = "completed"
        record["vector_output"] = outcome.get("vector_output")
        check = outcome.get("result_check") or {}
        record["features"] = check.get("feature_count")
        record["acres"] = check.get("crop_acres")
        record["aoi_acres"] = check.get("aoi_acres")
        record["retention_pct"] = check.get("static_retention_pct")
        record["warnings"] = check.get("warnings")
        vector = outcome.get("vector_output")
        if vector and Path(vector).exists():
            import geopandas as gpd
            gdf = gpd.read_file(vector)
            record["gpkg_rows"] = int(len(gdf))
            record["gpkg_columns"] = list(gdf.columns)
    except BaseException as exc:  # noqa: BLE001
        record["status"] = "stopped"
        record["error_type"] = type(exc).__name__
        record["error"] = str(exc)
    record["seconds"] = round(time.time() - started, 1)
    print("\n--- case result ---")
    print(json.dumps(record, indent=2, default=str), flush=True)
    return record


def main() -> int:
    results = [
        case("1 spr_maize 2027 -- year outside the archive, static path",
             "V3_sprmaize_2027", "spr_maize", "2027",
             str(AOIS / "okara_test_data_spr_maize.shp")),
        case("2 rice 2027 -- year outside the archive, NDVI-only path",
             "V3_rice_2027", "rice", "2027",
             str(AOIS / "okara_test_data_rice.shp")),
        case("3 cane on a real sub-AOI with genuinely no cane",
             "V3_nocane_block", "cane", "2025",
             str(AOIS / "okara_nocane_block.shp")),
    ]
    (ROOT / "metrics" / "verify_r2_3.json").write_text(json.dumps(results, indent=2, default=str))
    print("\nwrote metrics/verify_r2_3.json")

    maize, rice, nocane = results
    ok = True

    def assert_(name, condition, detail=""):
        nonlocal ok
        ok = ok and bool(condition)
        print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))

    print("\n=== verdict ===")
    assert_("spr_maize 2027 stops rather than reporting a result",
            maize["status"] == "stopped", f"{maize['status']}: {maize.get('error','')[:90]}")
    assert_("rice 2027 (NDVI-only) stops too -- this is the path that used to ship a zero",
            rice["status"] == "stopped", f"{rice['status']}: {rice.get('error','')[:90]}")
    assert_("the 2027 error names it an acquisition failure, not an absent crop",
            "acquisition failure" in rice.get("error", "").lower()
            or "no imagery" in rice.get("error", "").lower(),
            rice.get("error", "")[:120])
    assert_("the real crop-free sub-AOI still completes",
            nocane["status"] == "completed", f"{nocane['status']}: {nocane.get('error','')[:90]}")
    assert_("it still gets an empty GPKG with the full schema",
            nocane.get("gpkg_rows") == 0
            and nocane.get("gpkg_columns") == ["predicted", "area_acres", "geometry"],
            f"rows={nocane.get('gpkg_rows')} cols={nocane.get('gpkg_columns')}")
    assert_("it still returns a path, not None",
            bool(nocane.get("vector_output")), str(nocane.get("vector_output"))[-60:])
    assert_("the two outcomes are genuinely different",
            maize["status"] != nocane["status"] and rice["status"] != nocane["status"],
            f"2027={rice['status']} / crop-free={nocane['status']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
