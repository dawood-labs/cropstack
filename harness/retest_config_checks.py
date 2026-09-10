"""Fast, run-free retest checks: window order, era policy, AOI handling."""
import sys, traceback
sys.path.insert(0, "/home/jovyan/FAO/optimized_code_testing/cropstack")
from config import build_pipeline_config

AOI = "/home/jovyan/FAO/optimized_code_testing/test_aois_small"
KEY = "/home/jovyan/FAO/optimized_code_testing/gcs_data_downloader_ee_farmdar.json"
def cfg(crop, year, aoi=None, **kw):
    return build_pipeline_config(crop, year, "okara",
        aoi_path=aoi or f"{AOI}/okara_test_data_{crop}.shp",
        gee_service_account_key=KEY, **kw)

print("="*74); print("WINDOW ORDER (documented order must match resolved order)"); print("="*74)
DOC = {
 ("cane",None):        [("11-07","11-15"),("11-01","11-06"),("10-15","10-31"),("11-16","11-25")],
 ("wheat","punjab"):   [("02-10","02-25"),("01-25","02-10"),("02-26","03-10"),("03-11","03-20")],
 ("wheat","sindh"):    [("02-01","02-20"),("01-20","01-31"),("02-21","02-29")],
 ("spr_maize",None):   [("05-01","05-10"),("04-20","04-30"),("05-11","05-20")],
}
for (crop, region), doc in DOC.items():
    kw = {"region": region} if region else {}
    got = cfg(crop, "2025", **kw).resolved_static_windows()
    exp = [(f"2025-{a}", f"2025-{b}") for a,b in doc]
    exp = [(a, b if not (b.endswith("02-29")) else "2025-02-28") for a,b in exp]
    ok = got == exp
    print(f"{crop:10s} region={str(region):8s} {'MATCH' if ok else 'MISMATCH'}")
    for i,w in enumerate(got,1): print(f"    {i}. {w[0]} -> {w[1]}")
    if not ok: print(f"    expected: {exp}")

print()
print("leap-year clamp, wheat/sindh window 3:")
for y in ("2024","2025"):
    print(f"   {y}: {cfg('wheat', y, region='sindh').resolved_static_windows()[2]}")

print()
print("unknown region must raise:")
try:
    cfg("wheat","2025",region="balochistan").resolved_static_windows()
    print("   NO ERROR RAISED  <-- PROBLEM")
except Exception as e:
    print(f"   {type(e).__name__}: {e}")

print(); print("="*74); print("SENSOR ERAS"); print("="*74)
c = cfg("cane","2014")
print(f"2014 default      : ndvi_source={c.ndvi_source!r} run_static_model={c.run_static_model} "
      f"sentinel2_available={c.sentinel2_available}")
try:
    c2 = cfg("cane","2014", ndvi_source="stac"); c2.validate()
    print("2014 ndvi=stac    : NO ERROR RAISED  <-- PROBLEM")
except Exception as e:
    print(f"2014 ndvi=stac    : {type(e).__name__}: {e}")
c3 = cfg("cane","2016", static_source="gee")
print(f"2016 static=gee   : run_static_model={c3.run_static_model} uses_landsat_static={c3.uses_landsat_static}")
c4 = cfg("cane","2016", ndvi_source="gee", static_source="stac")
print(f"2016 gee+stac     : ndvi={c4.ndvi_source} static={c4.static_source} run_static={c4.run_static_model}")
try:
    c5 = cfg("cane","2016", static_source="gee", run_static_model=True); c5.validate()
    print("2016 gee forced   : NO ERROR RAISED  <-- PROBLEM")
except Exception as e:
    print(f"2016 gee forced   : {type(e).__name__}: {e}")
