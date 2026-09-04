"""Cheap unit-level coverage of paths a full pipeline run does not reach:
extra AOI formats, the Windows-path repair, run tags and explicit run ids."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path

REPO = Path("/home/jovyan/FAO/optimized_code_testing/cropstack")
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import geopandas as gpd  # noqa: E402
import aoi_io  # noqa: E402
import run_manager  # noqa: E402

SRC = Path("/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_cane.shp")
WORK = Path("/home/jovyan/FAO/optimized_code_testing/aoi_variants")
WORK.mkdir(parents=True, exist_ok=True)
results = []


def record(name, ok, detail):
    results.append({"name": name, "result": "PASS" if ok else "FAIL", "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")


# ------------------------------------------------------------------ AOI formats
gdf = gpd.read_file(SRC)
reference_bounds = [round(v, 6) for v in gdf.total_bounds]

format_specs = [("kml", "KML"), ("fgb", "FlatGeobuf"), ("parquet", None), ("geojson", "GeoJSON")]
for extension, driver in format_specs:
    target = WORK / f"okara_test_data_cane.{extension}"
    try:
        if extension == "parquet":
            gdf.to_parquet(target)
        else:
            gdf.to_file(target, driver=driver)
    except Exception as exc:
        record(f"aoi format .{extension} (write)", False, f"could not write: {exc}")
        continue
    try:
        resolved = aoi_io.resolve_aoi(target)
        # `resolve_aoi` normalises to an OGR-readable path, so a .parquet input comes
        # back as the converted .gpkg. Reading the *resolved* path as parquet asserted
        # something resolve_aoi never promised, and failed on its own converted output.
        loaded = gpd.read_file(resolved)
        same = [round(v, 6) for v in loaded.total_bounds] == reference_bounds
        record(f"aoi format .{extension}", same,
               f"resolved -> {resolved.name}, n={len(loaded)}, bounds match={same}")
    except Exception as exc:
        record(f"aoi format .{extension}", False, f"{type(exc).__name__}: {exc}")

# zipped shapefile
zip_path = WORK / "okara_cane_zipped.zip"
with zipfile.ZipFile(zip_path, "w") as archive:
    for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        sidecar = SRC.with_suffix(extension)
        if sidecar.exists():
            archive.write(sidecar, sidecar.name)
try:
    resolved = aoi_io.resolve_aoi(zip_path, cache_dir=WORK / "zipcache")
    loaded = gpd.read_file(resolved)
    same = [round(v, 6) for v in loaded.total_bounds] == reference_bounds
    record("aoi format .zip (shapefile inside)", same,
           f"extracted -> {resolved.name}, n={len(loaded)}, bounds match={same}")
except Exception as exc:
    record("aoi format .zip (shapefile inside)", False, f"{type(exc).__name__}: {exc}")

# missing sidecar must fail loudly
tmp = Path(tempfile.mkdtemp())
shutil.copy(SRC, tmp / "lonely.shp")
try:
    aoi_io.resolve_aoi(tmp / "lonely.shp")
    record("shapefile missing .shx/.dbf", False, "resolve_aoi accepted a shapefile with no sidecars")
except FileNotFoundError as exc:
    record("shapefile missing .shx/.dbf", True, f"raised FileNotFoundError: {exc}")
except Exception as exc:
    record("shapefile missing .shx/.dbf", False, f"raised {type(exc).__name__} instead: {exc}")

# ------------------------------------------------------- Windows path handling
mangled = "C:\\data\\aoi\fao_cane.shp"       # \f already eaten by Python
repaired = aoi_io.repair_mangled_windows_path(mangled)
record("windows escape repair (\\f -> formfeed)", repaired == "C:\\data\\aoi\\fao_cane.shp",
       f"{mangled!r} -> {repaired!r}")

spellings = aoi_io.translate_path_spellings("C:\\data\\aoi.shp")
record("windows -> WSL/GitBash spellings",
       "/mnt/c/data/aoi.shp" in spellings and "/c/data/aoi.shp" in spellings,
       f"{spellings}")

record("normalize strips quotes/whitespace/r-prefix",
       aoi_io.normalize_path_text('  r"/tmp/x.shp"  ') == "/tmp/x.shp",
       repr(aoi_io.normalize_path_text('  r"/tmp/x.shp"  ')))

# ------------------------------------------------------------ run folder logic
sandbox = Path(tempfile.mkdtemp())
d1, id1, reuse1 = run_manager.resolve_stage_dir(sandbox, run_manager.STAGE_NDVI, "resume")
(d1 / "marker.txt").write_text("x")
d2, id2, reuse2 = run_manager.resolve_stage_dir(sandbox, run_manager.STAGE_NDVI, "resume")
d3, id3, reuse3 = run_manager.resolve_stage_dir(sandbox, run_manager.STAGE_NDVI, "new")
d4, id4, _ = run_manager.resolve_stage_dir(sandbox, run_manager.STAGE_NDVI, "new", run_tag="cloudfix")
d5, id5, _ = run_manager.resolve_stage_dir(sandbox, run_manager.STAGE_NDVI, "2")
record("resume on empty dir starts at run 1", id1 == 1 and not reuse1, f"id={id1} reusing={reuse1}")
record("resume reuses a populated run", id2 == 1 and reuse2, f"id={id2} reusing={reuse2}")
record("new increments to max+1", id3 == 2, f"id={id3} -> {d3.name}")
record("run_tag is appended to a new folder", id4 == 3 and d4.name.endswith("_cloudfix"), d4.name)
record("explicit run id selects that run", id5 == 2 and d5.name.startswith("1_ndvi_run_2"), d5.name)

# resolve_stage_dir must not treat a tagged folder as a separate run number
runs = run_manager.existing_runs(sandbox, run_manager.STAGE_NDVI)
record("tagged folders are discovered by id", sorted(runs) == [1, 2, 3], f"{ {k: v.name for k, v in runs.items()} }")

print(f"\n{sum(1 for r in results if r['result']=='PASS')} passed, "
      f"{sum(1 for r in results if r['result']=='FAIL')} failed")
Path("/home/jovyan/FAO/optimized_code_testing/metrics/unit_tests.json").write_text(
    json.dumps(results, indent=2))
