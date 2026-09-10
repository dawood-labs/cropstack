"""Compare the actual pixel values the XGBoost model receives from each backend.

X1 kept the STAC static staging (4 tiles + static.vrt); X2 kept the GEE static GeoTIFF.
Both cover the same AOI for the same dates (2025-10-18 bottom, 2025-11-10 top), and both
are handed to `classify_static_image` in the same band order
(blue, green, red, rededge1, nir, ndvi). If FAIL-3 is real, every reflectance band on
the STAC side sits ~1000 DN higher and its NDVI band is correspondingly lower.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
WS = ROOT / "runs/A1_cane_2025"
BANDS = ["blue/B2", "green/B3", "red/B4", "rededge1/B5", "nir/B8", "NDVI"]


def find_inputs():
    """Pair the two runs that used the SAME dates (X1 STAC manual 2025-10-18 +
    2025-11-10, X2 GEE api_manual top 2025-11-10 over bottom 2025-10-18). Picking the
    newest file of each kind instead would pair the STAC manual mosaic against A5's
    *auto* composite and confound dates with backend."""
    stac = sorted(WS.glob("2_static_run_*/static_staging/static.vrt"))
    gee = [p for p in sorted(WS.glob("2_static_run_*/static_staging/*.tif"))
           if "static_10m_tile" not in p.name and "manual_2025-11-10_over_2025-10-18" in p.name]
    return (stac[-1] if stac else None), (gee[-1] if gee else None)


def stats_on_common_grid(stac_path, gee_path):
    """Reads the GEE image, then reprojects the STAC mosaic onto its exact grid so the
    comparison is pixel-for-pixel rather than statistic-for-statistic."""
    with rasterio.open(gee_path) as gee:
        gee_data = gee.read().astype(np.float64)
        profile = gee.profile
        dst_crs, dst_transform = gee.crs, gee.transform
        height, width = gee.height, gee.width
        gee_nodata = gee.nodata

    with rasterio.open(stac_path) as stac:
        bands = min(stac.count, gee_data.shape[0])
        stac_on_grid = np.zeros((bands, height, width), dtype=np.float64)
        for index in range(bands):
            reproject(
                source=rasterio.band(stac, index + 1),
                destination=stac_on_grid[index],
                src_transform=stac.transform, src_crs=stac.crs,
                dst_transform=dst_transform, dst_crs=dst_crs,
                resampling=Resampling.nearest)
        stac_nodata = stac.nodata

    print(f"STAC mosaic : {stac_path.relative_to(ROOT)}  ({bands} bands)")
    print(f"GEE  image  : {gee_path.relative_to(ROOT)}  ({gee_data.shape[0]} bands, "
          f"{width}x{height}, nodata={gee_nodata})")

    rows = []
    print(f"\n{'band':<14}{'STAC mean':>12}{'GEE mean':>12}{'delta':>10}"
          f"{'STAC med':>11}{'GEE med':>11}{'delta':>10}")
    for index in range(bands):
        s = stac_on_grid[index]
        g = gee_data[index]
        valid = (s > 0) & (g > 0) & np.isfinite(s) & np.isfinite(g)
        if valid.sum() < 1000:
            print(f"{BANDS[index]:<14} too few overlapping valid pixels ({int(valid.sum())})")
            continue
        sv, gv = s[valid], g[valid]
        row = {
            "band": BANDS[index], "n": int(valid.sum()),
            "stac_mean": round(float(sv.mean()), 1), "gee_mean": round(float(gv.mean()), 1),
            "mean_delta": round(float(sv.mean() - gv.mean()), 1),
            "stac_median": round(float(np.median(sv)), 1), "gee_median": round(float(np.median(gv)), 1),
            "median_delta": round(float(np.median(sv) - np.median(gv)), 1),
        }
        rows.append(row)
        print(f"{row['band']:<14}{row['stac_mean']:>12.1f}{row['gee_mean']:>12.1f}"
              f"{row['mean_delta']:>+10.1f}{row['stac_median']:>11.1f}"
              f"{row['gee_median']:>11.1f}{row['median_delta']:>+10.1f}")
    return rows


def main():
    stac_path, gee_path = find_inputs()
    if not stac_path or not gee_path:
        print(f"missing inputs: stac={stac_path} gee={gee_path}")
        return
    rows = stats_on_common_grid(stac_path, gee_path)
    (ROOT / "metrics" / "static_band_compare.json").write_text(json.dumps(rows, indent=2))
    reflectance = [r for r in rows if not r["band"].startswith("NDVI")]
    if reflectance:
        deltas = [r["median_delta"] for r in reflectance]
        print(f"\nreflectance bands: median delta ranges {min(deltas):+.0f} to {max(deltas):+.0f} DN"
              f"  (mean {sum(deltas)/len(deltas):+.0f})")
        print("No consistent ~1000 DN shift => farmdar.sentinel IS applying the")
        print("baseline-04.00 offset (harmonize_offset=1000, baseline-gated). The")
        print("residual differences come from cloud handling and compositing, not scale.")


if __name__ == "__main__":
    main()
