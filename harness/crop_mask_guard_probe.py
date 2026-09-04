"""`build_crop_mask` aborts when the mask covers >=90% of the static grid. That guard
protects against a mask that admits background classes -- but a legitimately homogeneous
AOI (one field, one crop) would look identical to it. How much headroom do real runs
have, and what does the failure look like?"""
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path("/home/jovyan/FAO/optimized_code_testing/cropstack")
sys.path.insert(0, str(REPO))

import static_classify  # noqa: E402

NDVI = ("/home/jovyan/FAO/optimized_code_testing/runs/A1_cane_2025/1_ndvi_run_1/"
        "okara_test_data_cane_rf_classification_map_sieved_p20.tif")
STATIC_GRID = NDVI          # same grid is fine for this probe
AOI = "/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_cane.shp"

print("observed crop-mask coverage in the real runs (guard trips at 90%):")
import re
for name in ("A1_cane_2025", "A2_cane_2025", "A3_cane_2025", "A4_cane_2025"):
    log = Path(f"/home/jovyan/FAO/optimized_code_testing/logs/{name}.log")
    if not log.exists():
        continue
    for line in log.read_text(errors="ignore").replace("\r", "\n").split("\n"):
        m = re.search(r"Crop mask coverage: ([\d,]+) px \(([\d.]+)%", line)
        if m:
            print(f"  {name:<18} {m.group(2):>6}%  ({m.group(1)} px)")
            break

tmp = Path(tempfile.mkdtemp())
print("\nforcing a degenerate mask (crop_classes = every class present):")
try:
    static_classify.build_crop_mask(
        static_image_path=STATIC_GRID, ndvi_classification_path=NDVI, aoi_path=AOI,
        output_mask_path=str(tmp / "mask.tif"), crop_classes=(1, 4), chunk_size=2048)
    print("  no error raised")
except AssertionError as exc:
    print(f"  AssertionError: {exc}")
except Exception:
    traceback.print_exc()
