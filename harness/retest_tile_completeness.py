"""Field-log fix: a short tile set must REFUSE, not mosaic with holes.

Two failure shapes are stubbed into farmdar's `fetch_sentinel_imagery` return value,
leaving the real acquired tiles on disk so the check runs against genuine files:
  (a) farmdar reports a tile as "failed"
  (b) farmdar reports more tiles than actually landed on disk (a silently dropped tile)
Control: the untouched result must still pass.
"""
import sys, shutil, tempfile
from pathlib import Path
sys.path.insert(0, "/home/jovyan/FAO/optimized_code_testing/cropstack")
from config import build_pipeline_config
import ndvi_pipeline

AOI = "/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_cane.shp"
KEY = "/home/jovyan/FAO/optimized_code_testing/gcs_data_downloader_ee_farmdar.json"
# real tiles acquired by the previous campaign
SRC = Path("/home/jovyan/FAO/optimized_code_testing/runs/W4_baseline_ndvionly_cane_2025/1_ndvi_run_1/raw_ndvi_tiles")

cfg = build_pipeline_config("cane", "2025", "okara", aoi_path=AOI,
                            gee_service_account_key=KEY, run_static_model=False)

real_tiles = sorted(SRC.glob("sentinel_*m_tile_*.tif"))
print(f"real tiles available: {len(real_tiles)}  ({[p.name for p in real_tiles]})\n")

def run_case(label, stub_result, drop_a_file):
    work = Path(tempfile.mkdtemp(prefix="tilecheck_"))
    for p in real_tiles:
        (work / p.name).symlink_to(p)
    if drop_a_file:
        (work / real_tiles[-1].name).unlink()
    ndvi_pipeline.fetch_sentinel_imagery = None  # ensure we patch the right import site
    import farmdar.sentinel as fs
    original = fs.fetch_sentinel_imagery
    fs.fetch_sentinel_imagery = lambda **kw: stub_result
    try:
        produced = ndvi_pipeline._acquire_tiles_from_stac(cfg, work)
        print(f"  {label:38s} -> RETURNED {len(produced)} tile(s)  <-- no refusal")
    except RuntimeError as exc:
        print(f"  {label:38s} -> RuntimeError: {str(exc)[:130]}")
    except BaseException as exc:
        print(f"  {label:38s} -> {type(exc).__name__}: {str(exc)[:120]}")
    finally:
        fs.fetch_sentinel_imagery = original
        shutil.rmtree(work, ignore_errors=True)

n = len(real_tiles)
ok = {"tiles": n, "results": [{"tile_id": f"000{i+1}", "status": "ok"} for i in range(n)]}
one_failed = {"tiles": n, "results": [{"tile_id": f"000{i+1}",
              "status": "failed: HTTPError 503"} if i == 2 else
              {"tile_id": f"000{i+1}", "status": "ok"} for i in range(n)]}

print("control  (all tiles present, all reported ok):")
run_case("untouched", ok, drop_a_file=False)
print("\n(a) farmdar reports one tile as failed:")
run_case("one tile status=failed", one_failed, drop_a_file=False)
print("\n(b) one tile file silently missing from disk:")
run_case("file count < reported tile count", ok, drop_a_file=True)
