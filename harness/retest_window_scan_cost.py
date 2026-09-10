"""Does scoring a priority window download imagery, and what does the scan cost?

Runs with nothing else on the box: psutil.net_io_counters() is system-wide, so the
idle baseline is measured first and reported alongside each window's delta.
"""
import sys, time, psutil
sys.path.insert(0, "/home/jovyan/FAO/optimized_code_testing/cropstack")
from config import build_pipeline_config
from static_pipeline import select_dates_by_priority

AOI = "/home/jovyan/FAO/optimized_code_testing/test_aois_small"
KEY = "/home/jovyan/FAO/optimized_code_testing/gcs_data_downloader_ee_farmdar.json"
MB = 1 << 20

def rx(): return psutil.net_io_counters().bytes_recv

print("idle baseline (5 s, nothing running):")
b0 = rx(); time.sleep(5); drift = (rx() - b0) / MB
print(f"  {drift:.2f} MB in 5 s  -> {drift/5:.3f} MB/s of ambient noise\n")

from farmdar.sentinel import select_static_dates
for crop, region in (("cane", None), ("wheat", "punjab"), ("spr_maize", None)):
    kw = {"region": region} if region else {}
    cfg = build_pipeline_config(crop, "2025", "okara",
        aoi_path=f"{AOI}/okara_test_data_{crop}.shp",
        gee_service_account_key=KEY, **kw)
    print(f"=== {crop} {('region='+region) if region else ''} ===")
    total_t = total_n = 0.0
    for i, (start, end) in enumerate(cfg.resolved_static_windows(), 1):
        n0, t0 = rx(), time.time()
        try:
            sel = select_static_dates(cfg.aoi_path, start, end, **cfg.stac_static_selection)
            cov, dates = sel.get("coverage_pct"), sel.get("dates")
        except Exception as exc:
            cov, dates = None, f"ERROR {exc}"
        dt_, dn = time.time() - t0, (rx() - n0) / MB
        total_t += dt_; total_n += dn
        print(f"  window {i} {start}..{end}: {dt_:5.1f} s  {dn:8.2f} MB  cov={cov}  {dates}")
    print(f"  -- scanning all {i} windows: {total_t:.1f} s, {total_n:.1f} MB "
          f"({total_n - drift/5*total_t:+.1f} MB above ambient)\n")
