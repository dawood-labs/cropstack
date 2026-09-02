"""Unified static-image pipeline: acquire (STAC auto/manual, or GEE
api_auto/api_manual/manual_gcs_link) -> mask by the NDVI result -> XGBoost classify ->
sieve. Skipped entirely when `cfg.run_static_model` is False.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

import postprocess
import static_classify
from config import PipelineConfig, STATIC_BAND_ORDER_GEE, STATIC_BAND_ORDER_STAC

logger = logging.getLogger(__name__)


def _format_date_suffix(dates) -> str:
    if not dates:
        return "unknown_date"
    return "_and_".join(datetime.strptime(d, "%Y-%m-%d").strftime("%d_%b_%Y") for d in dates)


def _acquire_static_from_stac(cfg: PipelineConfig, staging_dir: Path) -> Tuple[str, str]:
    """Returns (static image path, date suffix). In 'auto' mode the cloud-aware
    selector inside farmdar.sentinel picks the dates; in 'manual' mode the configured
    dates are fetched as-is."""
    # from farmdar.sentinel import fetch_sentinel_static_imagery  # never modified, only called
    from sentinel import fetch_sentinel_static_imagery

    if cfg.stac_static_mode == "manual":
        requested_dates = cfg.stac_static_dates
        selection_kwargs = {}
    else:
        # dates=None is what makes farmdar.sentinel run select_static_dates -- the
        # cloud-aware selection the original notebook silently skipped.
        requested_dates = None
        selection_kwargs = dict(cfg.stac_static_selection)

    result = fetch_sentinel_static_imagery(
        aoi=cfg.aoi_path,
        start=cfg.static_window_start,
        end=cfg.static_window_end,
        bands=STATIC_BAND_ORDER_STAC,
        out_dir=str(staging_dir),
        res_m=cfg.stac_resolution_m,
        tile_deg=cfg.stac_tile_size_deg,
        workers=cfg.stac_worker_count,
        build_vrt_mosaic=True,
        clip_to_aoi=False,
        dates=requested_dates,
        mask_clouds=False,
        **selection_kwargs,
    )

    selected_dates = result.get("dates", [])
    logger.info(f"STAC static dates ({cfg.stac_static_mode} mode): {selected_dates}")
    if cfg.stac_static_mode == "auto":
        selection = result.get("selection", {}) or {}
        logger.info(
            f"Date selection: anchor={selection.get('anchor')}, "
            f"AOI coverage={selection.get('coverage_pct')}%, metric={selection.get('cloud_metric')}"
        )

    image_path = result.get("vrt") or result.get("clipped")
    if not image_path:
        raise RuntimeError("STAC static acquisition returned no mosaic path.")
    return str(image_path), _format_date_suffix(selected_dates)


def _acquire_static_from_gee(cfg: PipelineConfig, staging_dir: Path) -> Tuple[str, str]:
    """Handles all three GEE static modes. `api_auto`/`api_manual` export through the
    Earth Engine API and download from GCS; `manual_gcs_link` skips the API entirely
    and just downloads a URI the user exported by hand from the GEE Code Editor.

    All three land on `gcs_io.download_gcs_object`, so GEE's sharded exports are
    reassembled identically no matter which mode produced them.
    """
    import gcs_io

    if cfg.gee_static_mode == "manual_gcs_link":
        logger.info(f"Downloading manually-exported static image: {cfg.gee_static_gcs_uri}")
        image_path = gcs_io.download_gcs_object(
            cfg.gee_static_gcs_uri, staging_dir, cfg.gee_service_account_key, max_workers=5,
        )
        return str(image_path), Path(image_path).stem

    import ee
    import geopandas as gpd

    import gee_client

    sensor_mode = cfg.gee_sensor_mode
    asset_base = Path(cfg.aoi_path).stem
    manual_mode = cfg.gee_static_mode == "api_manual"
    composite_type = "mosaic" if (manual_mode and cfg.gee_static_top_date) else "single"

    # The static composite is exported once over the AOI's bounding box (matching the
    # original notebooks' `fc.geometry().bounds()`), so no grid split or asset ingestion
    # is needed here -- a bounding box straight from the shapefile is sufficient, which
    # is what makes "GEE static + STAC NDVI" cheap to run.
    aoi = gpd.read_file(cfg.aoi_path).to_crs(4326)
    min_x, min_y, max_x, max_y = aoi.total_bounds
    features = ee.FeatureCollection([ee.Feature(ee.Geometry.Rectangle([min_x, min_y, max_x, max_y]))])

    auto_dates = "" if manual_mode else gee_client.pick_auto_composite_dates(
        features.geometry().bounds(), sensor_mode, cfg.static_window_start, cfg.static_window_end,
    )
    if auto_dates:
        logger.info(f"GEE auto-selected composite dates: {auto_dates.replace('_', ', ')}")

    output_filename = gee_client.static_composite_filename(
        asset_base, manual_mode, composite_type,
        cfg.gee_static_single_date, cfg.gee_static_top_date, cfg.gee_static_bottom_date,
        auto_dates=auto_dates,
    )
    timestamp = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y%m%d_%H%M%S")

    export_info = gee_client.export_static_composite(
        features, asset_base, timestamp, STATIC_BAND_ORDER_GEE, output_filename, cfg, sensor_mode,
        manual_mode=manual_mode, composite_type=composite_type,
        single_date=cfg.gee_static_single_date,
        top_date=cfg.gee_static_top_date,
        bottom_date=cfg.gee_static_bottom_date,
    )
    gee_client.wait_for_export_tasks([export_info["task_id"]])

    image_path = gcs_io.download_gcs_object(
        export_info["gcs_path"], staging_dir, cfg.gee_service_account_key, max_workers=5,
    )
    return str(image_path), Path(image_path).stem


def run_static_pipeline(
    cfg: PipelineConfig,
    out_dir: Path,
    sieved_ndvi_map_path: str,
    static_model_path: str,
) -> Optional[str]:
    """Acquires the static image from `cfg.static_source`, classifies it with XGBoost
    (restricted to the crop pixels the NDVI pipeline found), and sieves the result.
    Returns the sieved static classification path, or None when the static model is off.
    """
    if not cfg.run_static_model:
        logger.info("[Skipped] run_static_model is False.")
        return None

    out_dir = Path(out_dir)
    staging_dir = out_dir / "static_staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    if cfg.static_source == "stac":
        static_image_path, date_suffix = _acquire_static_from_stac(cfg, staging_dir)
    else:
        static_image_path, date_suffix = _acquire_static_from_gee(cfg, staging_dir)

    classified_dir = out_dir / date_suffix
    classified_dir.mkdir(parents=True, exist_ok=True)
    classified_path = classified_dir / f"static_mosaic_{date_suffix}_Cls.tif"
    crop_mask_path = classified_dir / f"{classified_path.stem}_crop_mask.tif"

    if classified_path.exists():
        logger.info(f"[Checkpoint] Static classification already exists: {classified_path}")
    else:
        static_classify.classify_static_image(
            static_image_path=static_image_path,
            output_path=str(classified_path),
            ndvi_classification_path=str(sieved_ndvi_map_path),
            aoi_path=str(cfg.aoi_path),
            model_path=str(static_model_path),
            crop_classes=cfg.ndvi_crop_classes,
            chunk_size=cfg.static_chunk_size,
            use_mask=True,
            model_positive_class=cfg.static_model_positive_class,
            crop_label=cfg.static_crop_label,
            background_label=cfg.static_background_label,
            worker_count=cfg.static_worker_count,
            output_nodata=cfg.static_output_nodata,
        )

    if cfg.delete_raw_static_tiles:
        shutil.rmtree(staging_dir, ignore_errors=True)
        crop_mask_path.unlink(missing_ok=True)
    else:
        logger.info(f"Raw static tiles kept at: {staging_dir} (crop mask: {crop_mask_path})")

    return postprocess.apply_strict_directional_sieve(
        input_raster_path=str(classified_path),
        target_classes=[cfg.static_crop_label],
        min_pixel_size=cfg.sieve_min_pixel_size,
        connectivity=4,
        nodata_val=cfg.static_background_label,
    )
