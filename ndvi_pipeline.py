"""Unified NDVI time-series pipeline: acquire (GEE or STAC) -> Whittaker + RF inference
-> mosaic -> sieve.

Acquisition is the only place the source matters; everything after it operates on local
GeoTIFF tiles via `inference_workers`, unchanged regardless of origin (see
`band_utils.parse_band_stack` for how both band-naming conventions are handled).
"""
from __future__ import annotations

import logging
import math
import time
import multiprocessing
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import List, Optional

import rasterio
from tqdm import tqdm

import postprocess
from config import PipelineConfig
from inference_workers import mosaic_prediction_tiles, worker_process_tile

logger = logging.getLogger(__name__)


def _tile_name(tile) -> str:
    """A tile's short name for a log line, without assuming it is a path.

    Diagnostics must never be the thing that fails: this runs on the error path, where
    raising would skip the pool cleanup that keeps a stall from becoming a hang.
    """
    try:
        return Path(tile).name
    except TypeError:
        return str(tile)


def _force_shutdown(pool) -> None:
    """Kills the pool's workers before shutting it down.

    A plain `shutdown()` joins the workers, which is the wrong thing to do when the
    reason we are here is that a worker stopped responding. Cancel what has not started,
    kill what has, then shut down without waiting.
    """
    for process in list(getattr(pool, "_processes", {}).values()):
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - best effort; the pool is already broken
            pass
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass


def run_tile_inference(tile_paths, task, task_args, worker_count: int,
                       max_tasks_per_worker: int, tile_timeout_s: int,
                       mp_context=None) -> tuple:
    """Runs `task` over every tile, recycling workers between batches.

    Two things this does not do, both learned from a district run that hung.

    It does not use `ProcessPoolExecutor(max_tasks_per_child=...)`. On this Python
    (3.11.15, spawn context) recycling a worker inside a live pool deadlocks: the run
    stops at exactly `workers x max_tasks_per_child` completed tasks, every worker gone,
    the parent waiting forever on a pipe from processes that no longer exist. Kasur
    stopped dead at 48 of 57 tiles with 6 workers and a limit of 8. A fresh pool per
    batch bounds worker memory the same way and never takes that path.

    And it does not put the timeout on `future.result()`. By the time `as_completed`
    yields a future that future has already completed, so the timeout there can never
    fire -- it looked like protection and was not. The budget belongs on `as_completed`
    itself, which is what actually blocks.

    Returns `(results, failed_tiles)`.
    """
    context = mp_context or multiprocessing.get_context("spawn")
    batch_size = max(1, worker_count * max(1, max_tasks_per_worker))
    # Each worker takes at most `max_tasks_per_worker` tiles in sequence, so this is the
    # longest a healthy batch can legitimately run.
    batch_timeout_s = max(1, max_tasks_per_worker) * max(1, tile_timeout_s)

    results, failed_tiles = [], []
    batches = [tile_paths[start:start + batch_size]
               for start in range(0, len(tile_paths), batch_size)]
    completed = 0
    with tqdm(total=len(tile_paths), desc="NDVI tiles") as progress:
        for batch_number, batch in enumerate(batches, start=1):
            if len(batches) > 1:
                logger.info(f"Inference batch {batch_number}/{len(batches)}: "
                            f"{len(batch)} tile(s) on {worker_count} fresh worker(s).")
            # Not a `with` block: on the stall path its __exit__ would call
            # shutdown(wait=True) and join the very workers that are not responding,
            # turning the timeout back into the hang it exists to prevent.
            pool = ProcessPoolExecutor(max_workers=worker_count, mp_context=context)
            futures = {pool.submit(task, *task_args(tile)): tile for tile in batch}
            try:
                for future in as_completed(futures, timeout=batch_timeout_s):
                    tile_path = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:  # noqa: BLE001
                        failed_tiles.append(tile_path)
                        logger.error(f"Tile failed: {_tile_name(tile_path)}: {exc}")
                    finally:
                        completed += 1
                        progress.update(1)
            except FuturesTimeoutError:
                # Shut the pool down before anything that could itself fail. An exception
                # raised while building the message would skip the cleanup and leave the
                # workers running, and the interpreter would then hang joining them at
                # exit -- the same hang, reached by a different road.
                _force_shutdown(pool)
                unfinished = [_tile_name(tile) for future, tile in futures.items()
                              if not future.done()]
                raise RuntimeError(
                    f"NDVI inference stalled in batch {batch_number}/{len(batches)} after "
                    f"{completed} of {len(tile_paths)} tile(s): {len(unfinished)} tile(s) "
                    f"made no progress in {batch_timeout_s}s ({unfinished[:5]}). Finished "
                    "tiles are on disk, so re-run with run_mode='resume' to continue from "
                    "here rather than starting the acquisition again."
                ) from None
            except BaseException:
                _force_shutdown(pool)
                raise
            pool.shutdown(wait=True)
    return results, failed_tiles


def per_tile_minutes(outcomes, elapsed_minutes: float, tile_total: int,
                     worker_count: int) -> tuple:
    """How long a tile actually took, and on what evidence.

    Wall-clock over tile count is throughput, not per-tile cost: eight workers divide it
    by eight, so tiles genuinely taking 2.4 min each read as 0.8 and no threshold worth
    setting could fire. farmdar reports each tile's own duration, so prefer that; when it
    does not, divide by the number of sequential batches rather than by tiles.

    Returns `(minutes_per_tile, basis)` -- the basis is logged, because a number whose
    meaning changes with the worker count is worth naming.
    """
    reported = [float(outcome[key]) / 60 for outcome in outcomes or []
                for key in ("seconds", "duration", "elapsed")
                if isinstance(outcome.get(key), (int, float))]
    if reported:
        return sum(reported) / len(reported), "mean of farmdar's per-tile durations"

    workers = max(1, worker_count)
    batches = max(1, math.ceil(max(1, tile_total) / workers))
    return elapsed_minutes / batches, f"wall-clock over {batches} batch(es) of {workers} worker(s)"


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

    started_at = time.time()
    result = fetch_sentinel_imagery(
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
    # Concurrent tile requests have been observed to drop tiles. farmdar reports each
    # tile's fate, so check it: a silently short tile set becomes a district map with
    # holes that still exits 0.
    outcomes = result.get("results") or []

    # Tile throughput is the one number that distinguishes "this AOI is large" from
    # "every request is being retried". Expired temporary credentials and Azure 502s both
    # show up here as minutes per tile; the retries themselves happen inside
    # farmdar.sentinel, which we do not modify, so surfacing the rate is what we can do.
    elapsed_minutes = (time.time() - started_at) / 60
    tile_total = result.get("tiles") or len(outcomes) or 1

    minutes_per_tile, basis = per_tile_minutes(
        outcomes, elapsed_minutes, tile_total, _stac_worker_budget(cfg))
    logger.info(f"STAC acquisition took {elapsed_minutes:.1f} min for {tile_total} tile(s) "
                f"-- {minutes_per_tile:.1f} min/tile ({basis}).")
    if minutes_per_tile > cfg.stac_slow_tile_warning_minutes:
        logger.warning(
            f"Tiles averaged {minutes_per_tile:.1f} min each, well above the "
            f"{cfg.stac_slow_tile_warning_minutes:.0f} min expected. The usual causes are "
            "expired temporary credentials (ResponseParserError on an empty response) and "
            "upstream 502s from the catalogue, both of which are retried internally and so "
            "only appear as slowness. Refresh credentials and re-run with "
            "run_mode='resume' rather than waiting it out."
        )

    failed = [r for r in outcomes if str(r.get("status", "")).startswith("failed")]
    expected = result.get("tiles")
    produced = sorted(tiles_dir.glob("sentinel_*m_tile_*.tif"))

    if failed:
        raise RuntimeError(
            f"STAC acquisition failed for {len(failed)} of {len(outcomes)} tile(s): "
            f"{[(r.get('tile_id'), r.get('status')) for r in failed[:5]]}. "
            "Re-run with run_mode='resume' to retry only the missing tiles."
        )
    if expected and len(produced) < expected:
        raise RuntimeError(
            f"STAC acquisition returned {len(produced)} tile file(s) but reported "
            f"{expected} tile(s). The mosaic would have holes. Re-run with "
            "run_mode='resume' to fetch the rest."
        )

    return produced


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


def _assert_classification_has_data(classification_path, cfg) -> None:
    """Refuses a classification map that is nodata everywhere.

    A year outside the archive returns tiles that carry no pixels; every stage then
    succeeds on empty arrays and the run ends with a clean zero-acre result and no
    warning. An absent year and a genuinely crop-free district must not produce the same
    output, so this fails rather than reports.
    """
    with rasterio.open(classification_path) as src:
        for _, window in src.block_windows(1):
            if bool((src.read(1, window=window) != cfg.ndvi_nodata_label).any()):
                return

    raise RuntimeError(
        f"The NDVI classification for {cfg.crop} {cfg.year} is nodata in every pixel: the "
        f"acquisition returned no imagery over this AOI for "
        f"{cfg.ndvi_series_start}..{cfg.ndvi_series_end}. Check the year is within the "
        "archive and the AOI is where you think it is -- this is an acquisition failure, "
        "not a district without crop."
    )


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

        # The model is passed by path: each worker loads it once and caches it, instead
        # of the whole RandomForest being pickled into every task.
        outcomes, failed_tiles = run_tile_inference(
            tile_paths,
            worker_process_tile,
            lambda tile_path: (tile_path, predictions_dir, str(ndvi_model_path),
                               cfg.ndvi_inference_start, cfg.ndvi_inference_end,
                               0.5, 2, (-1.0, 1.0), cfg.ndvi_nodata_label, False),
            worker_count=worker_count,
            max_tasks_per_worker=cfg.ndvi_worker_max_tasks,
            tile_timeout_s=cfg.ndvi_tile_timeout_s,
        )
        prediction_paths = [outcome["prediction"] for outcome in outcomes]

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
        _assert_classification_has_data(classification_path, cfg)
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
