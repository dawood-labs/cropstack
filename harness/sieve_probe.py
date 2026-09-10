"""Why does the static-stage sieve remove nothing?

apply_strict_directional_sieve is called with nodata_val = static_background_label (4)
for the static stage, which makes `valid_mask = data != 4` exclude every background
pixel. GDAL's sieve replaces a small blob with its largest *valid* neighbour -- and a
small class-1 blob surrounded by masked-out class-4 has no valid neighbour, so nothing
can ever be removed. The NDVI stage passes 255, which no pixel holds, so its mask is
all-True and the sieve behaves normally.
"""
import numpy as np
import rasterio
from rasterio.features import sieve

CLS = ("runs/A1_cane_2025/2_static_run_1/16_Oct_2025/static_mosaic_16_Oct_2025_Cls.tif")
NDVI = ("runs/A1_cane_2025/1_ndvi_run_1/okara_test_data_cane_rf_classification_map.tif")


def probe(path, nodata_val, label):
    with rasterio.open(path) as src:
        data = src.read(1)
    before = int((data == 1).sum())

    masked_mask = data != nodata_val
    sieved_masked = sieve(data, size=20, connectivity=4, mask=masked_mask)
    after_masked = int((sieved_masked == 1).sum())

    sieved_unmasked = sieve(data, size=20, connectivity=4)
    after_unmasked = int((sieved_unmasked == 1).sum())

    print(f"\n{label}")
    print(f"  file                  : {path.split('/')[-1]}")
    print(f"  nodata_val passed     : {nodata_val}")
    print(f"  pixels masked OUT     : {int((~masked_mask).sum()):,} of {data.size:,} "
          f"({100*(~masked_mask).sum()/data.size:.1f}%)")
    print(f"  class-1 px before     : {before:,}")
    print(f"  class-1 px AS CALLED  : {after_masked:,}   (delta {after_masked-before:+,})")
    print(f"  class-1 px if unmasked: {after_unmasked:,}   (delta {after_unmasked-before:+,})")
    print(f"  -> sieve as called is {'A NO-OP' if after_masked == before else 'effective'}")
    return {"before": before, "as_called": after_masked, "unmasked": after_unmasked}


print("=" * 70)
static = probe(CLS, 4, "STATIC stage  (nodata_val = static_background_label = 4)")
ndvi = probe(NDVI, 255, "NDVI stage    (nodata_val = ndvi_nodata_label = 255)")
print("=" * 70)
print(f"\nStatic sieve removed {static['before']-static['as_called']:,} px as called, "
      f"but would remove {static['before']-static['unmasked']:,} px if the background "
      f"were not masked out.")
print(f"NDVI sieve removed {ndvi['before']-ndvi['as_called']:,} px "
      f"({100*(ndvi['before']-ndvi['as_called'])/ndvi['before']:.1f}% of the class).")
