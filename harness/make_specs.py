"""Generates every scenario spec JSON used by the test matrix."""
import json
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
AOI = ROOT / "test_aois_small"
KEY = str(ROOT / "gcs_data_downloader_ee_farmdar.json")
RUNS = ROOT / "runs"
SPECS = ROOT / "specs"
SPECS.mkdir(exist_ok=True)

# Cloud-free acquisition dates confirmed present in the Sentinel-2 L2A archive over this
# AOI (queried from the Planetary Computer STAC before the runs).
MANUAL_DATES = {
    ("cane", "2025"): ["2025-10-18", "2025-11-10"],
    ("wheat", "2025"): ["2025-02-03", "2025-02-23"],
    ("spr_maize", "2025"): ["2025-05-01", "2025-05-13"],
    ("cane", "2016"): ["2016-10-15", "2016-11-14"],
    ("wheat", "2016"): ["2016-02-01", "2016-02-18"],
    ("spr_maize", "2016"): ["2016-05-05", "2016-05-15"],
}


def base(crop, year, name, **extra):
    spec = {
        "crop": crop, "year": year, "district_name": "okara",
        "aoi_path": str(AOI / f"okara_test_data_{crop}.shp"),
        "gee_service_account_key": KEY,
        "output_dir": str(RUNS / name),
        "ndvi_source": "stac", "static_source": "stac", "stac_static_mode": "auto",
    }
    spec.update(extra)
    return spec


def write(name, spec):
    (SPECS / f"{name}.json").write_text(json.dumps(spec, indent=2))
    return name


written = []

# ---- Scenario A: source matrix -------------------------------------------------
# The brief's four rows cross `stac_static_mode` with static_source="gee" and
# `gee_static_mode` with static_source="stac", which are no-ops; and row 4 names
# static_source="stack" / gee_static_mode="manual", neither of which is a valid value
# (both are covered as fail-fast cases in config_tests.py). Run the four *meaningful*
# static acquisition modes those rows were evidently aiming at.
for crop in ("cane", "wheat", "spr_maize"):
    for year in ("2025", "2016"):
        tag = f"{crop}_{year}"
        written.append(write(f"A1_{tag}", base(crop, year, f"A1_{tag}",
                       static_source="stac", stac_static_mode="auto")))
        written.append(write(f"A2_{tag}", base(crop, year, f"A2_{tag}",
                       static_source="stac", stac_static_mode="manual",
                       stac_static_dates=MANUAL_DATES[(crop, year)])))
        written.append(write(f"A3_{tag}", base(crop, year, f"A3_{tag}",
                       static_source="gee", gee_static_mode="api_auto")))
        written.append(write(f"A4_{tag}", base(crop, year, f"A4_{tag}",
                       static_source="gee", gee_static_mode="api_manual",
                       gee_static_bottom_date=MANUAL_DATES[(crop, year)][0],
                       gee_static_top_date=MANUAL_DATES[(crop, year)][1])))

# ---- Scenario B: rice, NDVI-only ------------------------------------------------
for year in ("2025", "2016"):
    written.append(write(f"B_rice_{year}", base("rice", year, f"B_rice_{year}")))

# ---- Scenario C: run folders / resume -------------------------------------------
# All share ONE output dir on purpose: that is what makes run numbering observable.
c_dir = str(RUNS / "C_runfolders_cane_2025")
for name, extra in [
    ("C1_first_resume", dict(run_mode="resume")),
    ("C2_second_resume", dict(run_mode="resume")),
    ("C3_new", dict(run_mode="new")),
    ("C5_resume_after_kill", dict(run_mode="resume")),
    ("C6_ndvi_resume_static_new", dict(ndvi_run_mode="resume", static_run_mode="new")),
]:
    spec = base("cane", "2025", "C_runfolders_cane_2025", **extra)
    spec["output_dir"] = c_dir
    written.append(write(name, spec))

# C4: the run that gets SIGKILLed mid-NDVI. Its own dir so the kill cannot damage C1-C3.
kill_dir = str(RUNS / "C_kill_cane_2025")
for name in ("C4a_kill_target", "C4b_resume_after_kill"):
    spec = base("cane", "2025", "C_kill_cane_2025", run_mode="resume")
    spec["output_dir"] = kill_dir
    # Keep the raw tiles so a resume can be observed reusing them.
    spec["delete_raw_ndvi_tiles"] = False
    written.append(write(name, spec))

# ---- Scenario D: AOI input formats ----------------------------------------------
for fmt in ("shp", "gpkg", "geojson"):
    name = f"D_{fmt}_cane_2025"
    spec = base("cane", "2025", name, run_static_model=False)
    spec["aoi_path"] = str(AOI / f"okara_test_data_cane.{fmt}")
    written.append(write(name, spec))
# gs:// AOI: deliberately NO output_dir, so default_output_dir's GCS branch is exercised.
spec = base("cane", "2025", "D_gcs_cane_2025", run_static_model=False)
spec["aoi_path"] = "gs://farmdar_data_catalog/fao_pipeline_test_aois/okara_test_data_cane.shp"
spec["base_dir"] = str(RUNS / "D_gcs_base")
spec.pop("output_dir")
written.append(write("D_gcs_cane_2025", spec))

# ---- Worker-setting variants for the worst stage (scenario 5) --------------------
written.append(write("W1_cane_2025_workers2", base("cane", "2025", "W1_cane_2025_workers2",
                     ndvi_worker_count=2, ndvi_worker_max_tasks=1)))
written.append(write("W2_cane_2025_workers8", base("cane", "2025", "W2_cane_2025_workers8",
                     ndvi_worker_count=8, ndvi_worker_max_tasks=8, static_worker_count=8,
                     static_chunk_size=4096)))

print(f"wrote {len(written)} specs")
for name in written:
    print("  ", name)
