"""Scenario F: prove the outputs are right, not merely that the exit code was 0."""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
METRICS = ROOT / "metrics"
ACRES = 0.000247105
AREA_CRS = 32642

EXPECTED_LABEL = {"cane": 1, "wheat": 14, "spr_maize": 3015, "rice": 1}
# (crop-class label, background label) written into the static classification
STATIC_LABELS = {"cane": (1, 4), "wheat": (1, 4), "spr_maize": (1, 8), "rice": None}
NDVI_CLASSES = {"cane": [1], "wheat": [14], "spr_maize": [1, 4, 5, 6, 7], "rice": [1]}

AOI = {
    "cane": ROOT / "test_aois_small/okara_test_data_cane.shp",
    "wheat": ROOT / "test_aois_small/okara_test_data_wheat.shp",
    "spr_maize": ROOT / "test_aois_small/okara_test_data_spr_maize.shp",
    "rice": ROOT / "test_aois_small/okara_test_data_rice.shp",
}


def raster_facts(path):
    with rasterio.open(path) as src:
        raw = src.read(1)
        masked = src.read(1, masked=True)
        values, counts = np.unique(raw, return_counts=True)
        present = {int(v): int(c) for v, c in zip(values, counts)}
        visible = set(int(v) for v in np.unique(masked.compressed())) if masked.count() else set()
        return {
            "nodata": src.nodata, "crs": str(src.crs), "dtype": str(src.dtypes[0]),
            "size": [src.width, src.height],
            "labels": present,
            "hidden_by_mask": sorted(set(present) - visible),
        }


def crop_acres_inside_aoi(raster_path, labels, aoi_path):
    """Acreage of `labels` in the raster, restricted to the AOI polygon -- the upper
    bound the vector stage can possibly produce from this raster."""
    import rasterio.mask
    aoi = gpd.read_file(aoi_path)
    with rasterio.open(raster_path) as src:
        shapes = aoi.to_crs(src.crs).geometry.values
        clipped, _ = rasterio.mask.mask(src, shapes, crop=True, filled=True,
                                        nodata=255 if src.nodata is None else src.nodata)
        band = clipped[0]
        pixel_m2 = abs(src.transform.a) * abs(src.transform.e)
        if src.crs and src.crs.to_epsg() == 4326:
            # degrees -> metres at this latitude, good enough for a plausibility bound
            import math
            lat = aoi.to_crs(4326).geometry.iloc[0].centroid.y
            pixel_m2 = (abs(src.transform.a) * 111320 * math.cos(math.radians(lat))) * \
                       (abs(src.transform.e) * 110540)
    count = int(np.isin(band, labels).sum())
    return count * pixel_m2 * ACRES


def check(name):
    result = json.loads((METRICS / f"{name}_result.json").read_text())
    if result.get("status") != "ok":
        return {"scenario": name, "status": result.get("status"), "error": result.get("error")}
    outcome = result["outcome"]
    crop = outcome["crop"]
    row = {"scenario": name, "status": "ok", "crop": crop, "year": outcome["year"],
           "problems": [], "warnings": []}

    ndvi = raster_facts(outcome["sieved_ndvi_raster"])
    row["ndvi_raster"] = ndvi
    if ndvi["nodata"] != 255:
        row["problems"].append(f"NDVI sieved raster nodata is {ndvi['nodata']}, expected 255")
    # 255 IS the NDVI map's genuine nodata, so it is *supposed* to disappear under a
    # masked read. Only a real class label going missing is a defect.
    hidden_classes = [v for v in ndvi["hidden_by_mask"] if v != (ndvi["nodata"] or -1)]
    if hidden_classes:
        row["problems"].append(f"NDVI class labels hidden by nodata mask: {hidden_classes}")
    if 255 in ndvi["labels"]:
        row["warnings"].append(
            f"NDVI map contains {ndvi['labels'][255]:,} nodata (255) pixels "
            f"({100*ndvi['labels'][255]/sum(ndvi['labels'].values()):.1f}% of the grid) "
            "-- tiles with no usable signal in the inference window")
    if not set(NDVI_CLASSES[crop]) & set(ndvi["labels"]):
        row["problems"].append(f"NDVI raster holds none of the crop classes {NDVI_CLASSES[crop]}")

    if outcome.get("sieved_static_raster"):
        static = raster_facts(outcome["sieved_static_raster"])
        row["static_raster"] = static
        crop_label, background_label = STATIC_LABELS[crop]
        missing = [lab for lab in (crop_label, background_label) if lab not in static["labels"]]
        if missing:
            row["problems"].append(f"static raster missing label(s) {missing}")
        hidden_static = [v for v in static["hidden_by_mask"] if v != (static["nodata"] or -1)]
        if hidden_static:
            row["problems"].append(
                f"static class labels hidden by nodata mask: {hidden_static}")

    vector_path = outcome.get("vector_output")
    if not vector_path or not Path(vector_path).exists():
        row["problems"].append("no vector output produced")
        return row

    gdf = gpd.read_file(vector_path)
    aoi = gpd.read_file(AOI[crop])
    aoi_acres = float(aoi.to_crs(AREA_CRS).area.sum() * ACRES)
    area = gdf.to_crs(AREA_CRS).area * ACRES
    row["vector"] = {
        "path": vector_path,
        "features": len(gdf),
        "crs": str(gdf.crs),
        "total_acres": round(float(area.sum()), 2),
        "min_acres": round(float(area.min()), 4) if len(gdf) else None,
        "invalid_geoms": int((~gdf.geometry.is_valid).sum()),
        "empty_geoms": int(gdf.geometry.is_empty.sum()),
        "geom_types": sorted(gdf.geom_type.unique().tolist()),
        "predicted": sorted(set(int(v) for v in gdf["predicted"].unique())) if "predicted" in gdf else None,
        "aoi_acres": round(aoi_acres, 2),
        "pct_of_aoi": round(100 * float(area.sum()) / aoi_acres, 2),
    }
    v = row["vector"]
    if len(gdf) == 0:
        row["problems"].append("GPKG is empty")
    if v["invalid_geoms"]:
        row["problems"].append(f"{v['invalid_geoms']} invalid geometries")
    if v["empty_geoms"]:
        row["problems"].append(f"{v['empty_geoms']} empty geometries")
    if v["predicted"] != [EXPECTED_LABEL[crop]]:
        row["problems"].append(
            f"predicted label {v['predicted']} != output_polygon_label {EXPECTED_LABEL[crop]}")
    if v["pct_of_aoi"] > 100:
        row["problems"].append(f"acreage exceeds the AOI ({v['pct_of_aoi']}%)")
    if v["min_acres"] is not None and v["min_acres"] < 0.5 - 1e-6:
        row["problems"].append(f"polygon below the 0.5-acre threshold ({v['min_acres']})")
    if str(gdf.crs).upper() not in ("EPSG:4326",):
        row["problems"].append(f"unexpected vector CRS {gdf.crs}")

    # Consistency gate: the vector must actually derive from THIS run's source raster.
    # Clipping and the 0.5-acre filter can only shrink the raster's in-AOI crop area,
    # typically to 80-95% of it -- a far smaller ratio means the GPKG came from
    # different raster data (a resumed vector folder returning a previous run's file).
    source_raster = outcome.get("sieved_static_raster") or outcome["sieved_ndvi_raster"]
    labels = ([STATIC_LABELS[crop][0]] if outcome.get("sieved_static_raster")
              else NDVI_CLASSES[crop])
    try:
        upper_bound = crop_acres_inside_aoi(source_raster, labels, AOI[crop])
        ratio = v["total_acres"] / upper_bound if upper_bound else float("nan")
        v["source_raster_in_aoi_acres"] = round(upper_bound, 2)
        v["vector_over_raster_ratio"] = round(ratio, 3)
        # Deterministic staleness signal: the vector stage announced it reused an
        # existing file. On its own that is fine (a pure re-run); combined with a
        # vector/raster ratio far below the usual 0.85-1.0 it means the returned GPKG
        # was built from different raster data than this run produced.
        log_path = ROOT / "logs" / f"{name}.log"
        reused = False
        if log_path.exists():
            reused = "[Skipped] Vectorised outputs already exist" in log_path.read_text(
                errors="ignore")
        v["vector_stage_reused_existing_file"] = reused
        if reused and upper_bound > 0 and ratio < 0.75:
            row["problems"].append(
                f"STALE vector: the vector stage reused an existing GPKG and its "
                f"acreage is only {ratio:.2f}x this run's own source raster "
                f"({v['total_acres']:.1f} vs {upper_bound:.1f} acres in AOI)")
        elif upper_bound > 0 and ratio < 0.6:
            row["warnings"].append(
                f"vector keeps only {ratio:.2f}x its source raster's in-AOI crop area "
                f"-- heavily fragmented classification (see the static-sieve no-op finding)")
        if upper_bound > 0 and ratio > 1.05:
            row["problems"].append(
                f"vector acreage exceeds its source raster ({ratio:.2f}x)")
    except Exception as exc:
        v["source_raster_check_error"] = f"{type(exc).__name__}: {exc}"
    return row


def main():
    names = sorted(p.name[: -len("_result.json")] for p in METRICS.glob("*_result.json"))
    rows = [check(n) for n in names]
    (METRICS / "final_validation.json").write_text(json.dumps(rows, indent=2, default=str))

    head = (f"{'scenario':<26}{'crop':<11}{'feats':>7}{'acres':>10}{'%AOI':>7}"
            f"{'pred':>7}{'inval':>6}{'v/r':>7}  verdict")
    print(head)
    print("-" * (len(head) + 20))
    for row in rows:
        if row.get("status") != "ok":
            print(f"{row['scenario']:<26}{'-':<11}{'':>7}{'':>10}{'':>7}{'':>7}{'':>6}  "
                  f"RUN FAILED: {row.get('error')}")
            continue
        v = row.get("vector")
        if row["problems"]:
            verdict = "FAIL: " + "; ".join(row["problems"])
        elif row.get("warnings"):
            verdict = "PASS (warn): " + "; ".join(row["warnings"])
        else:
            verdict = "PASS"
        if v:
            ratio = v.get("vector_over_raster_ratio")
            print(f"{row['scenario']:<26}{row['crop']:<11}{v['features']:>7}"
                  f"{v['total_acres']:>10.1f}{v['pct_of_aoi']:>7.1f}"
                  f"{str(v['predicted']):>7}{v['invalid_geoms']:>6}"
                  f"{(ratio if ratio is not None else float('nan')):>7.2f}  {verdict}")
        else:
            print(f"{row['scenario']:<26}{row['crop']:<11}{'-':>7}{'-':>10}{'-':>7}{'-':>7}{'-':>6}  {verdict}")
    bad = [r for r in rows if r.get("problems")]
    print(f"\n{len(rows)-len(bad)} of {len(rows)} scenarios fully clean; {len(bad)} with problems")


if __name__ == "__main__":
    main()
