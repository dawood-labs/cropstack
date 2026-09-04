"""Post-run plausibility checks.

There is no ground truth at run time, so nothing here can say an answer is right. What
it can do is put the numbers that reveal an implausible answer in front of the operator
instead of leaving them to be discovered a week later: what share of the AOI was called
crop, and how much of the NDVI stage's crop area the static model kept.

Both were real failures in the field -- wheat was over-predicted in Thatta, and a static
run has retained essentially all of its input mask (the model contributing nothing) as
well as almost none of it (the model rejecting a hazy image wholesale). Neither showed
up in any log.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Union

import geopandas as gpd
import numpy as np
import rasterio

logger = logging.getLogger(__name__)

AREA_CRS_EPSG = 32642
ACRES_PER_SQ_METRE = 0.000247105


def _aoi_acres(aoi_path: Union[str, Path]) -> Optional[float]:
    try:
        aoi = gpd.read_file(aoi_path)
        if aoi.empty:
            return None
        return float(aoi.geometry.to_crs(epsg=AREA_CRS_EPSG).area.sum() * ACRES_PER_SQ_METRE)
    except Exception as exc:
        logger.debug(f"Could not measure the AOI: {exc}")
        return None


def _count_labelled_pixels(raster_path: Union[str, Path], labels) -> Optional[int]:
    """Counts by block, so a district-sized raster never lands in memory whole."""
    try:
        wanted = np.asarray(list(labels))
        total = 0
        with rasterio.open(raster_path) as src:
            for _, window in src.block_windows(1):
                block = src.read(1, window=window)
                total += int(np.isin(block, wanted).sum())
        return total
    except Exception as exc:
        logger.debug(f"Could not count pixels in {raster_path}: {exc}")
        return None


def assess_result(
    vector_path: Optional[Union[str, Path]],
    aoi_path: Union[str, Path],
    ndvi_raster: Optional[Union[str, Path]] = None,
    ndvi_crop_classes=(1,),
    static_raster: Optional[Union[str, Path]] = None,
    static_crop_label: int = 1,
    max_crop_share_pct: Optional[float] = None,
    min_crop_share_pct: Optional[float] = None,
    min_static_retention_pct: Optional[float] = 5.0,
    max_static_retention_pct: Optional[float] = 99.5,
    report_path: Optional[Union[str, Path]] = None,
) -> dict:
    """Measures the delivered result and warns when it looks implausible. Advisory only:
    it never fails a run, because a genuinely low-cropped district is a valid answer."""
    report: dict = {"warnings": []}

    def warn(message: str) -> None:
        report["warnings"].append(message)
        logger.warning("RESULT CHECK: " + message)

    aoi_acres = _aoi_acres(aoi_path)
    report["aoi_acres"] = round(aoi_acres, 1) if aoi_acres else None

    if vector_path and Path(vector_path).exists():
        try:
            result = gpd.read_file(vector_path)
            report["feature_count"] = int(len(result))
            crop_acres = float(result["area_acres"].sum()) if "area_acres" in result and len(result) else 0.0
            report["crop_acres"] = round(crop_acres, 1)
            if aoi_acres:
                share = 100.0 * crop_acres / aoi_acres
                report["crop_share_of_aoi_pct"] = round(share, 1)
                if max_crop_share_pct is not None and share > max_crop_share_pct:
                    warn(f"{share:.1f}% of the AOI was classified as crop, above the "
                         f"{max_crop_share_pct:.0f}% plausible ceiling for this district. "
                         "Over-prediction here has come from regional phenology drift; "
                         "check the date and the model against local knowledge.")
                if min_crop_share_pct is not None and share < min_crop_share_pct:
                    warn(f"Only {share:.1f}% of the AOI was classified as crop, below the "
                         f"{min_crop_share_pct:.0f}% plausible floor. A hazy or partly "
                         "covered static image suppresses the crop class this way.")
        except Exception as exc:
            logger.debug(f"Could not read the vector result: {exc}")

    if ndvi_raster and static_raster:
        ndvi_pixels = _count_labelled_pixels(ndvi_raster, ndvi_crop_classes)
        static_pixels = _count_labelled_pixels(static_raster, [static_crop_label])
        report["ndvi_crop_pixels"] = ndvi_pixels
        report["static_crop_pixels"] = static_pixels
        if ndvi_pixels and static_pixels is not None:
            retention = 100.0 * static_pixels / ndvi_pixels
            report["static_retention_pct"] = round(retention, 1)
            if min_static_retention_pct is not None and retention < min_static_retention_pct:
                warn(f"The static model kept only {retention:.1f}% of the NDVI stage's crop "
                     "area. That is the signature of a hazy or cloud-contaminated image "
                     "rather than of a real crop boundary -- re-run from the next window "
                     "(static_window_start_at) before accepting this.")
            elif max_static_retention_pct is not None and retention > max_static_retention_pct:
                warn(f"The static model kept {retention:.1f}% of the NDVI stage's crop area, "
                     "so it removed essentially nothing. Check that the right static model "
                     "loaded and that the image is the intended date.")

    logger.info(
        "Result: "
        + ", ".join(f"{key}={value}" for key, value in report.items() if key != "warnings")
        + (f" -- {len(report['warnings'])} warning(s)" if report["warnings"] else "")
    )

    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    return report
