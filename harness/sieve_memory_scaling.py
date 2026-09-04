"""How much RAM do the sieve and vectorize steps need per pixel?

Both load the whole raster into memory, so their cost is set by the raster's pixel
count, not by the AOI's crop area. The test AOI is 2226x2226 (5.0 Mpx); a district at
10 m is easily 1-3 Gpx, so the per-pixel figure is what decides whether these steps fit
in RAM at production size. Measured by tiling the real classification raster up to a
larger synthetic raster and watching peak RSS.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import rasterio

REPO = Path("/home/jovyan/FAO/optimized_code_testing/cropstack")
HARNESS = Path("/home/jovyan/FAO/optimized_code_testing/harness")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HARNESS))

from instrument import ResourceSampler  # noqa: E402
import postprocess  # noqa: E402

SRC = Path("/home/jovyan/FAO/optimized_code_testing/runs/A1_cane_2025/1_ndvi_run_1/"
           "okara_test_data_cane_rf_classification_map.tif")
SCRATCH = Path("/tmp/claude-1000/-home-jovyan-FAO-optimized-code-testing/"
               "d4421094-41a5-461c-b80f-491152d02357/scratchpad/sievebench")
METRICS = Path("/home/jovyan/FAO/optimized_code_testing/metrics")


def make_tiled(factor):
    """Writes a raster `factor`x larger in each axis by tiling the real one."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    out = SCRATCH / f"tiled_{factor}x.tif"
    with rasterio.open(SRC) as src:
        data = src.read(1)
        profile = src.profile.copy()
        transform = src.transform
    big = np.tile(data, (factor, factor))
    profile.update(driver="GTiff", height=big.shape[0], width=big.shape[1],
                   compress="lzw", tiled=True, blockxsize=256, blockysize=256,
                   bigtiff="YES", transform=transform)
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(big, 1)
    pixels = big.size
    del big, data
    return out, pixels


def bench(factor):
    raster, pixels = make_tiled(factor)
    sieved = raster.parent / f"{raster.stem}_sieved_p20{raster.suffix}"
    sieved.unlink(missing_ok=True)

    sampler = ResourceSampler(csv_path=METRICS / f"bench_sieve_{factor}x_samples.csv").start()
    baseline = sampler.rows[0]["rss_tree_mb"] if sampler.rows else 0
    time.sleep(1.0)
    baseline = min(r["rss_tree_mb"] for r in sampler.rows) if sampler.rows else 0

    sampler.mark("sieve")
    started = time.time()
    postprocess.apply_strict_directional_sieve(
        input_raster_path=str(raster), target_classes=[1],
        min_pixel_size=20, connectivity=4, nodata_val=255)
    sieve_s = time.time() - started
    sieve_peak = sampler.peak_rss_tree_mb

    sampler.mark("vectorize")
    started = time.time()
    postprocess.vectorize_process_and_export(
        input_raster_path=str(sieved),
        boundary_shp_path="/home/jovyan/FAO/optimized_code_testing/test_aois_small/okara_test_data_cane.shp",
        output_dir=str(SCRATCH / f"vec_{factor}x"), output_basename=f"bench_{factor}x",
        target_labels=[1], relabel_as=1, min_area_acres=0.5, save_shp_zip=False)
    vector_s = time.time() - started
    total_peak = sampler.peak_rss_tree_mb
    sampler.stop()

    row = {
        "factor": factor, "pixels": pixels,
        "megapixels": round(pixels / 1e6, 1),
        "baseline_mb": round(baseline, 1),
        "sieve_s": round(sieve_s, 1),
        "sieve_peak_mb": round(sieve_peak, 1),
        "sieve_bytes_per_px": round((sieve_peak - baseline) * 1e6 / pixels, 2),
        "vector_s": round(vector_s, 1),
        "overall_peak_mb": round(total_peak, 1),
        "overall_bytes_per_px": round((total_peak - baseline) * 1e6 / pixels, 2),
    }
    for path in (raster, sieved):
        path.unlink(missing_ok=True)
    return row


def main():
    # One factor per PROCESS: peak RSS is only comparable against a clean baseline, and
    # CPython does not return freed pages promptly, so a second bench in the same
    # process starts from an inflated floor.
    if "--single" in sys.argv:
        factor = int(sys.argv[sys.argv.index("--single") + 1])
        row = bench(factor)
        (METRICS / f"sieve_bench_{factor}x.json").write_text(json.dumps(row, indent=2))
        print(json.dumps(row))
        return

    import subprocess
    factors = [int(x) for x in (sys.argv[1:] or ["1", "3", "5"])]
    rows = []
    for factor in factors:
        print(f"\n--- {factor}x ---", flush=True)
        subprocess.run([sys.executable, __file__, "--single", str(factor)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        path = METRICS / f"sieve_bench_{factor}x.json"
        if not path.exists():
            print("  (failed)")
            continue
        row = json.loads(path.read_text())
        rows.append(row)
        print(f"  {row['megapixels']} Mpx | sieve {row['sieve_s']}s peak {row['sieve_peak_mb']} MB "
              f"({row['sieve_bytes_per_px']} B/px) | vectorize {row['vector_s']}s | "
              f"overall {row['overall_bytes_per_px']} B/px", flush=True)

    (METRICS / "sieve_memory_scaling.json").write_text(json.dumps(rows, indent=2))
    print(f"\n{'Mpx':>9}{'sieve_s':>9}{'sieve_peak_MB':>15}{'sieve_B/px':>12}"
          f"{'vector_s':>10}{'overall_B/px':>14}")
    for row in rows:
        print(f"{row['megapixels']:>9.1f}{row['sieve_s']:>9.1f}{row['sieve_peak_mb']:>15.0f}"
              f"{row['sieve_bytes_per_px']:>12.2f}{row['vector_s']:>10.1f}"
              f"{row['overall_bytes_per_px']:>14.2f}")
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        slope = ((last["overall_peak_mb"] - first["overall_peak_mb"]) * 1e6
                 / (last["pixels"] - first["pixels"]))
        print(f"\nmarginal cost (baseline-free): {slope:.2f} bytes per pixel")
    if rows:
        bpp = max(r["overall_bytes_per_px"] for r in rows)
        print("\nextrapolated peak RAM for the sieve+vectorize steps alone:")
        for mpx in (100, 500, 1000, 2500):
            print(f"  {mpx:>5} Mpx raster: {mpx * 1e6 * bpp / 1e9:6.1f} GB")


if __name__ == "__main__":
    main()
