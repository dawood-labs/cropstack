"""Correctness checks on a finished run's outputs.

Exit code 0 from the pipeline is not evidence of a correct product, so this inspects
the actual rasters and vectors: which class labels survive a masked read, whether the
NDVI map keeps its genuine nodata, and whether the GPKG is non-empty, valid, sanely
projected and of a plausible acreage against the AOI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import geopandas as gpd
import numpy as np
import rasterio

ACRES_PER_SQ_METRE = 0.000247105
AREA_CRS_EPSG = 32642


def describe_raster(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return info
    info["size_mb"] = round(path.stat().st_size / 1e6, 2)
    with rasterio.open(path) as src:
        info.update({
            "driver": src.driver, "dtype": str(src.dtypes[0]), "count": src.count,
            "width": src.width, "height": src.height,
            "crs": str(src.crs), "nodata": src.nodata,
            "res": [round(v, 4) for v in src.res],
        })
        raw = src.read(1)
        values, counts = np.unique(raw, return_counts=True)
        info["unique_raw"] = {int(v): int(c) for v, c in zip(values, counts)}

        masked = src.read(1, masked=True)
        info["masked_count_valid"] = int(masked.count())
        info["masked_count_total"] = int(masked.size)
        if masked.count():
            mvalues, mcounts = np.unique(masked.compressed(), return_counts=True)
            info["unique_masked"] = {int(v): int(c) for v, c in zip(mvalues, mcounts)}
        else:
            info["unique_masked"] = {}
        # Labels that exist in the raw data but vanish under a masked read are the
        # symptom of a real class being tagged as nodata.
        info["labels_hidden_by_mask"] = sorted(
            set(info["unique_raw"]) - set(info["unique_masked"])
        )
    return info


def describe_vector(path: Path, aoi_path: str, expected_label: int) -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return info
    info["size_mb"] = round(path.stat().st_size / 1e6, 2)
    gdf = gpd.read_file(path)
    info["n_features"] = len(gdf)
    info["columns"] = list(gdf.columns)
    info["crs"] = str(gdf.crs)
    if gdf.empty:
        info["empty"] = True
        return info
    info["empty"] = False
    info["n_invalid_geoms"] = int((~gdf.geometry.is_valid).sum())
    info["n_empty_geoms"] = int(gdf.geometry.is_empty.sum())
    info["geom_types"] = sorted(gdf.geom_type.unique().tolist())

    area_acres = gdf.geometry.to_crs(epsg=AREA_CRS_EPSG).area * ACRES_PER_SQ_METRE
    info["total_acres"] = round(float(area_acres.sum()), 2)
    info["min_acres"] = round(float(area_acres.min()), 4)
    info["max_acres"] = round(float(area_acres.max()), 2)
    info["median_acres"] = round(float(area_acres.median()), 3)
    if "area_acres" in gdf.columns:
        info["stored_total_acres"] = round(float(gdf["area_acres"].sum()), 2)
    if "predicted" in gdf.columns:
        info["predicted_values"] = sorted(set(int(v) for v in gdf["predicted"].unique()))
        info["predicted_matches_expected"] = info["predicted_values"] == [int(expected_label)]

    aoi = gpd.read_file(aoi_path)
    aoi_acres = float(aoi.to_crs(epsg=AREA_CRS_EPSG).area.sum() * ACRES_PER_SQ_METRE)
    info["aoi_acres"] = round(aoi_acres, 2)
    info["pct_of_aoi"] = round(100.0 * info["total_acres"] / aoi_acres, 2) if aoi_acres else None
    # A crop covering >100% of the AOI is impossible; near-0% is suspicious.
    info["plausible"] = bool(0.0 < info["total_acres"] <= aoi_acres * 1.02)
    return info


def scan_run(out_dir: Path, aoi_path: str, expected_label: int) -> Dict[str, Any]:
    report: Dict[str, Any] = {"output_dir": str(out_dir), "rasters": [], "vectors": []}
    if not out_dir.exists():
        report["error"] = "output dir does not exist"
        return report

    raster_paths: List[Path] = []
    for pattern in ("*_rf_classification_map.tif", "*_sieved_p*.tif", "*_Cls.tif", "*crop_mask.tif"):
        raster_paths.extend(sorted(out_dir.rglob(pattern)))
    seen = set()
    for path in raster_paths:
        if path in seen:
            continue
        seen.add(path)
        report["rasters"].append(describe_raster(path))

    for path in sorted(out_dir.rglob("*.gpkg")):
        report["vectors"].append(describe_vector(path, aoi_path, expected_label))

    report["run_info"] = {}
    for path in sorted(out_dir.rglob("run_info.json")):
        try:
            report["run_info"][str(path.relative_to(out_dir))] = json.loads(path.read_text())
        except Exception as exc:
            report["run_info"][str(path)] = f"unreadable: {exc}"

    report["zips"] = [str(p.relative_to(out_dir)) for p in sorted(out_dir.rglob("*.zip"))]
    report["tree"] = sorted(
        str(p.relative_to(out_dir)) + ("/" if p.is_dir() else f"  ({p.stat().st_size/1e6:.2f} MB)")
        for p in out_dir.rglob("*") if p.is_dir() or p.is_file()
    )[:400]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--label", type=int, required=True)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    report = scan_run(Path(args.out_dir), args.aoi, args.label)
    text = json.dumps(report, indent=2, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
