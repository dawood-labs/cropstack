"""Scenario D, gs:// leg: verify a GCS-sourced AOI never writes outputs into the
disposable AOI cache.

A real gs:// run was not possible in this sandbox (uploading a test AOI to the shared
bucket was blocked), so this exercises the exact guard that decides the question --
`pipeline.default_output_dir` -- against a config shaped exactly as a gs:// run leaves
it: aoi_source is the gs:// URI and aoi_path points inside the AOI cache.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path("/home/jovyan/FAO/optimized_code_testing/cropstack")
sys.path.insert(0, str(REPO))
os.chdir(REPO)

from config import DEFAULT_AOI_CACHE_DIR, PipelineConfig  # noqa: E402
from pipeline import default_output_dir  # noqa: E402

CACHE = Path(os.path.expanduser(DEFAULT_AOI_CACHE_DIR))
LOCAL_AOI = "/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_cane.shp"

results = []


def check(name, cfg, must_avoid_cache: bool):
    out = default_output_dir(cfg)
    inside_cache = CACHE.resolve() in out.resolve().parents or out.resolve() == CACHE.resolve()
    ok = (not inside_cache) if must_avoid_cache else True
    results.append({
        "name": name, "output_dir": str(out),
        "inside_aoi_cache": inside_cache,
        "verdict": "PASS" if ok else "FAIL",
    })
    print(f"{name}\n    -> {out}\n    inside AOI cache: {inside_cache}   {'PASS' if ok else 'FAIL'}")


# 1. Exactly the state a gs:// AOI leaves behind: cached file + gs:// aoi_source.
cached_aoi = CACHE / "farmdar_data_catalog" / "fao_pipeline_test_aois" / "okara_test_data_cane.shp"
cfg = PipelineConfig(
    crop="cane", year="2025", district_name="okara",
    aoi_path=str(cached_aoi),
    aoi_source="gs://farmdar_data_catalog/fao_pipeline_test_aois/okara_test_data_cane.shp",
    base_dir="/home/jovyan/FAO/optimized_code_testing/runs/D_gcs_base",
    ndvi_crop_classes=[1],
)
check("gs:// AOI (aoi_source is gs://, aoi_path in cache)", cfg, must_avoid_cache=True)

# 2. Cache path but aoi_source not a URI -- the second half of the guard (`cache_root in
#    aoi_path.parents`) must still catch it.
cfg2 = PipelineConfig(
    crop="cane", year="2025", district_name="okara",
    aoi_path=str(cached_aoi), aoi_source=str(cached_aoi),
    base_dir="/home/jovyan/FAO/optimized_code_testing/runs/D_gcs_base",
    ndvi_crop_classes=[1],
)
check("AOI physically inside the cache, no gs:// source", cfg2, must_avoid_cache=True)

# 3. Ordinary local AOI -- outputs belong next to the AOI.
cfg3 = PipelineConfig(
    crop="cane", year="2025", district_name="okara",
    aoi_path=LOCAL_AOI, aoi_source=LOCAL_AOI, ndvi_crop_classes=[1],
)
check("local AOI (expected: alongside the AOI)", cfg3, must_avoid_cache=True)
expected = str(Path(LOCAL_AOI).parent / "cane_2025")
print(f"    expected alongside-AOI location: {expected}   "
      f"{'MATCH' if results[-1]['output_dir'] == expected else 'MISMATCH'}")
results[-1]["matches_alongside_aoi"] = results[-1]["output_dir"] == expected

# 4. explicit output_dir must win over both branches.
cfg4 = PipelineConfig(
    crop="cane", year="2025", district_name="okara",
    aoi_path=str(cached_aoi),
    aoi_source="gs://bucket/aoi.shp",
    output_dir="/tmp/explicit_override", ndvi_crop_classes=[1],
)
check("explicit output_dir overrides the gs:// branch", cfg4, must_avoid_cache=True)

out = Path("/home/jovyan/FAO/optimized_code_testing/metrics/gcs_aoi_guard_test.json")
out.write_text(json.dumps(results, indent=2))
print(f"\nwrote {out}")
print("OVERALL:", "PASS" if all(r["verdict"] == "PASS" for r in results) else "FAIL")
