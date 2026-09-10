"""Second-pass specs: static-mode variants reuse one NDVI stage per crop/year.

A2/A3/A4 differ from A1 only in how the *static* image is acquired, so they share the
crop/year workspace and run with ndvi_run_mode="resume" + static_run_mode="new" +
vector_run_mode="new". That both saves the ~20 min NDVI acquisition per variant and
exercises the documented "keep the expensive NDVI result, redo only the static stage"
path directly.
"""
import json
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
SPECS = ROOT / "specs"
RUNS = ROOT / "runs"

MANUAL_DATES = {
    ("cane", "2025"): ["2025-10-18", "2025-11-10"],
    ("wheat", "2025"): ["2025-02-03", "2025-02-23"],
    ("spr_maize", "2025"): ["2025-05-01", "2025-05-13"],
    ("cane", "2016"): ["2016-10-15", "2016-11-14"],
}

REUSE = dict(ndvi_run_mode="resume", static_run_mode="new", vector_run_mode="new")


def load(name):
    return json.loads((SPECS / f"{name}.json").read_text())


def save(name, spec):
    (SPECS / f"{name}.json").write_text(json.dumps(spec, indent=2))
    print(f"  {name}: out={Path(spec['output_dir']).name} "
          f"static={spec.get('static_source')}/"
          f"{spec.get('stac_static_mode') if spec.get('static_source')=='stac' else spec.get('gee_static_mode')}")


print("static-mode variants sharing a workspace per crop/year:")
for crop, year in [("cane", "2025"), ("cane", "2016"), ("wheat", "2025"), ("spr_maize", "2025")]:
    tag = f"{crop}_{year}"
    workspace = str(RUNS / f"A1_{tag}")   # A1 creates it; A2-A4 resume its NDVI stage
    for variant, extra in [
        ("A2", dict(static_source="stac", stac_static_mode="manual",
                    stac_static_dates=MANUAL_DATES[(crop, year)])),
        ("A3", dict(static_source="gee", gee_static_mode="api_auto")),
        ("A4", dict(static_source="gee", gee_static_mode="api_manual",
                    gee_static_bottom_date=MANUAL_DATES[(crop, year)][0],
                    gee_static_top_date=MANUAL_DATES[(crop, year)][1])),
    ]:
        name = f"{variant}_{tag}"
        spec = load(name)
        spec["output_dir"] = workspace
        spec.update(extra)
        spec.update(REUSE)
        save(name, spec)

# Worker-setting experiments: run inside a workspace whose raw tiles are kept, so only
# the inference settings differ rather than re-paying acquisition.
print("\nworker experiments:")
for name, extra in [
    ("W1_cane_2025_workers2", dict(ndvi_worker_count=2, ndvi_worker_max_tasks=1)),
    ("W2_cane_2025_workers8", dict(ndvi_worker_count=8, ndvi_worker_max_tasks=8,
                                   static_worker_count=8, static_chunk_size=4096)),
]:
    spec = load(name)
    spec["output_dir"] = str(RUNS / "W_cane_2025")
    spec["delete_raw_ndvi_tiles"] = False
    spec["run_static_model"] = False   # isolate the NDVI inference stage
    spec.update(extra)
    spec["ndvi_run_mode"] = "new"      # force a fresh inference each time
    spec["vector_run_mode"] = "new"
    save(name, spec)
