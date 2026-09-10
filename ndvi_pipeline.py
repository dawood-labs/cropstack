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


def _stac_worker_budget(cfg: PipelineConfig) -> int:
    """Trims STAC concurrency so the in-flight tiles fit in memory.

    Acquisition peak tracks (tiles in flight) x (tile area), not AOI size, so the
    defaults are safe on a small tile grid and can exhaust the box on a larger one --
    at tile_deg=0.2 the same 8 workers want roughly 4x the memory. Estimating first and
    trimming is cheaper than discovering it as an OOM 20 minutes into acquisition.
    """
    import math

    import geopandas as gpd

    import static_classify

    workers = cfg.stac_worker_count
    try:
        bounds = gpd.read_file(cfg.aoi_path).to_crs(4326).total_bounds
        tiles_across = max(1, math.ceil((bounds[2] - bounds[0]) / cfg.stac_tile_size_deg))
        tiles_down = max(1, math.ceil((bounds[3] - bounds[1]) / cfg.stac_tile_size_deg))
        tile_count = tiles_across * tiles_down
    except Exception:
        return workers

    in_flight = min(workers, tile_count)
    area_scale = (cfg.stac_tile_size_deg / 0.1) ** 2
    per_tile_gib = cfg.stac_tile_memory_gib * area_scale
    estimated_gib = in_flight * per_tile_gib

    available = static_classify.available_memory_bytes()
    if not available:
        logger.info(f"STAC acquisition: ~{tile_count} tile(s), estimated peak {estimated_gib:.1f} GiB")
        return workers

    budget_gib = (available / 2**30) * cfg.stac_memory_fraction
    if estimated_gib <= budget_gib:
        logger.info(
            f"STAC acquisition: ~{tile_count} tile(s), {in_flight} in flight, "
            f"estimated peak {estimated_gib:.1f} GiB of {budget_gib:.1f} GiB budget"
        )
        return workers

    affordable = max(1, int(budget_gib // per_tile_gib))
    logger.warning(
        f"STAC acquisition would need ~{estimated_gib:.1f} GiB "
        f"({in_flight} tiles in flight x {per_tile_gib:.1f} GiB) but only "
        f"{budget_gib:.1f} GiB is budgeted -- reducing stac_worker_count "
        f"{workers} -> {affordable}. Lower stac_tile_size_deg, or raise "
        f"stac_memory_fraction if this box can take it."
    )
    return affordable


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
        workers=_stac_worker_budget(cfg),
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
    # tiles_dir / predictions_dir are created only when work actually needs them, so a
    # resumed run does not leave empty scratch directories behind.
    out_dir.mkdir(parents=True, exist_ok=True)

    classification_path = out_dir / f"{Path(cfg.aoi_path).stem}_rf_classification_map.tif"

    if classification_path.exists():
        logger.info(f"[Checkpoint] NDVI classification map already exists: {classification_path}")
    else:
        tiles_dir.mkdir(parents=True, exist_ok=True)
        predictions_dir.mkdir(parents=True, exist_ok=True)
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

        # Parallelism is capped by tile count, not cores: extra workers would spawn,
        # load a model, and idle.
        worker_count = cfg.ndvi_worker_count or max(1, int(multiprocessing.cpu_count() * 0.75))
        worker_count = max(1, min(worker_count, len(tile_paths)))
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
