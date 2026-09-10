"""Unified NDVI time-series pipeline: acquire (GEE or STAC) -> Whittaker + RF inference
-> mosaic -> sieve.

Acquisition is the only place the source matters; everything after it operates on local
GeoTIFF tiles via `inference_workers`, unchanged regardless of origin (see
`band_utils.parse_band_stack` for how both band-naming conventions are handled).
"""
from __future__ import annotations

import logging
import multiprocessing
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from tqdm import tqdm

import postprocess
from config import PipelineConfig
from inference_workers import mosaic_prediction_tiles, worker_process_tile

logger = logging.getLogger(__name__)


def _acquire_tiles_from_stac(cfg: PipelineConfig, tiles_dir: Path) -> List[Path]:
    from farmdar.sentinel import fetch_sentinel_imagery  # never modified, only called
    # from sentinel import fetch_sentinel_imagery

    fetch_sentinel_imagery(
        aoi=cfg.aoi_path,
        start=cfg.ndvi_series_start,
        end=cfg.ndvi_series_end,
        bands=["red", "nir"],
        out_dir=str(tiles_dir),
        step=cfg.composite_step_days,
        res_m=cfg.stac_resolution_m,
        tile_deg=cfg.stac_tile_size_deg,
        cloud_lt=cfg.stac_ndvi_max_cloud_pct,
        workers=cfg.stac_worker_count,
        build_vrt_mosaic=False,   # tiles are consumed individually, no mosaic needed
        clip_to_aoi=False,
    )
    return sorted(tiles_dir.glob("sentinel_*m_tile_*.tif"))


def _acquire_tiles_from_gee(
    cfg: PipelineConfig, tiles_dir: Path, out_dir: Path, gee_credentials, gee_project: str,
) -> List[Path]:
    import gcs_io
    import gee_client

    sensor_mode = cfg.gee_sensor_mode
    grid_result = gee_client.split_aoi_into_grid(
        cfg.aoi_path,
        filename_suffix=f"{cfg.year}_{sensor_mode.capitalize()}",
        grid_cell_acres=cfg.gee_grid_cell_acres,
        save_grid_overlay=True,
        output_dir=str(out_dir),  # never write next to a GCS-cached AOI
    )
    gridded_aoi = grid_result["gridded_aoi"]
    gcs_folder = Path(gridded_aoi).stem

    gcs_gridded_uri = gee_client.upload_shapefile_to_gcs(
        gridded_aoi, cfg.gcs_bucket, cfg.gcs_base_folder, gcs_folder, gee_credentials, gee_project,
    )
    gee_asset_id = gee_client.ingest_shapefile_to_gee_asset(
        gcs_gridded_uri, f"projects/ee-{cfg.gee_project_name}/assets",
    )
    stack_uris = gee_client.export_ndvi_grid_stacks(gee_asset_id, cfg, sensor_mode)

    return [
        gcs_io.download_gcs_object(uri, tiles_dir, cfg.gee_service_account_key, max_workers=5)
        for uri in stack_uris
    ]


def run_ndvi_pipeline(
    cfg: PipelineConfig,
    out_dir: Path,
    ndvi_model_path: str,
    gee_credentials=None,
    gee_project: Optional[str] = None,
) -> str:
    """Acquires NDVI imagery from `cfg.ndvi_source`, runs Whittaker + RF inference per
    tile, mosaics, and sieves. Returns the sieved classification raster path."""
    out_dir = Path(out_dir)
    tiles_dir = out_dir / "raw_ndvi_tiles"
    predictions_dir = out_dir / "tile_predictions"
    for directory in (out_dir, tiles_dir, predictions_dir):
        directory.mkdir(parents=True, exist_ok=True)

    classification_path = out_dir / f"{Path(cfg.aoi_path).stem}_rf_classification_map.tif"

    if classification_path.exists():
        logger.info(f"[Checkpoint] NDVI classification map already exists: {classification_path}")
    else:
        if cfg.ndvi_source == "stac":
            tile_paths = _acquire_tiles_from_stac(cfg, tiles_dir)
        else:
            if gee_credentials is None or gee_project is None:
                raise ValueError(
                    "ndvi_source='gee' requires credentials from gee_client.init_gee_and_gcs()."
                )
            tile_paths = _acquire_tiles_from_gee(cfg, tiles_dir, out_dir, gee_credentials, gee_project)

        if not tile_paths:
            raise FileNotFoundError(f"NDVI acquisition produced no tiles in {tiles_dir}")

        worker_count = cfg.ndvi_worker_count or max(1, int(multiprocessing.cpu_count() * 0.75))
        logger.info(f"Running RF inference over {len(tile_paths)} tile(s) on {worker_count} worker(s)...")

        prediction_paths, failed_tiles = [], []
        # The model is passed by path: each worker loads it once and caches it, instead
        # of the whole RandomForest being pickled into every task.
        with ProcessPoolExecutor(
            max_workers=worker_count,
            max_tasks_per_child=cfg.ndvi_worker_max_tasks,
            mp_context=multiprocessing.get_context("spawn"),
        ) as pool:
            futures = {
                pool.submit(
                    worker_process_tile, tile_path, predictions_dir, str(ndvi_model_path),
                    cfg.ndvi_inference_start, cfg.ndvi_inference_end,
                    0.5, 2, (-1.0, 1.0), cfg.ndvi_nodata_label, False,
                ): tile_path
                for tile_path in tile_paths
            }
            for future in tqdm(as_completed(futures), total=len(futures), desc="NDVI tiles"):
                tile_path = futures[future]
                try:
                    prediction_paths.append(future.result(timeout=cfg.ndvi_tile_timeout_s)["prediction"])
                except Exception as exc:
                    failed_tiles.append(tile_path)
                    logger.error(f"Tile failed: {Path(tile_path).name}: {exc}")

        if not prediction_paths:
            raise RuntimeError(f"All {len(tile_paths)} NDVI tiles failed; no classification produced.")
        if failed_tiles:
            # Loud, because a silently-partial district map looks like a real result.
            logger.warning(
                f"{len(failed_tiles)} of {len(tile_paths)} tiles failed; the mosaic will have "
                f"holes. Failed tiles: {[Path(p).name for p in failed_tiles]}"
            )

        mosaic_prediction_tiles(
            prediction_paths, classification_path, nodata_label=cfg.ndvi_nodata_label,
        )
        shutil.rmtree(predictions_dir, ignore_errors=True)  # per-tile chunks are redundant once mosaicked

        if cfg.delete_raw_ndvi_tiles:
            shutil.rmtree(tiles_dir, ignore_errors=True)
        else:
            logger.info(f"Raw NDVI tiles kept at: {tiles_dir}")

    return postprocess.apply_strict_directional_sieve(
        input_raster_path=str(classification_path),
        target_classes=cfg.ndvi_crop_classes,
        min_pixel_size=cfg.ndvi_sieve_min_pixels,
        connectivity=4,
        nodata_val=cfg.ndvi_nodata_label,
    )
