"""End-to-end entry point: config in, final vector product out.

`run_pipeline(cfg)` is the whole thing -- validate, resolve models, acquire + infer
NDVI, acquire + infer the static image, vectorise and export. Both the driver notebook
and the batch runner call this, so there is exactly one definition of "the pipeline".
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import model_registry
import postprocess
import qc
import run_manager
from config import PipelineConfig
from ndvi_pipeline import run_ndvi_pipeline
from static_pipeline import run_static_pipeline

logger = logging.getLogger(__name__)


def default_output_dir(cfg: PipelineConfig) -> Path:
    """Where this run's outputs go.

    Normally alongside the AOI, which is what the original notebooks did. But an AOI
    fetched from GCS lives in the shared, disposable AOI cache -- writing results there
    would pollute a directory the user may clear at any time -- so those runs fall back
    to `base_dir/crop/district`. `cfg.output_dir` overrides both.
    """
    if cfg.output_dir:
        return Path(cfg.output_dir)

    aoi_path = Path(cfg.aoi_path).resolve()
    cache_root = Path(os.path.expanduser(cfg.aoi_cache_dir)).resolve()
    came_from_gcs = cfg.aoi_source.startswith("gs://") or cache_root in aoi_path.parents

    if came_from_gcs:
        return Path(cfg.base_dir) / cfg.crop / cfg.district_name / f"{cfg.crop}_{cfg.year}"
    return aoi_path.parent / f"{cfg.crop}_{cfg.year}"


def run_pipeline(
    cfg: PipelineConfig,
    out_dir: Optional[Path] = None,
    gee_credentials: Any = None,
    gee_project: Optional[str] = None,
    refresh_models: bool = False,
) -> Dict[str, Any]:
    """Runs the full pipeline for one AOI. Returns a dict of output paths and timings.

    Credentials are initialised automatically when the run needs them and none were
    passed in, so a caller (notebook cell or batch job) never has to think about which
    source combination requires Earth Engine.
    """
    started_at = time.time()
    cfg.validate()

    out_dir = Path(out_dir) if out_dir else default_output_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Pipeline configuration:\n%s", cfg.summary())

    # Each stage gets its own numbered folder, so re-running an AOI never collides with a
    # previous run and a crashed run can be resumed in place. The static stage is
    # resolved later, only if it actually runs.
    ndvi_dir, ndvi_run_id, ndvi_resumed = run_manager.resolve_stage_dir(
        out_dir, run_manager.STAGE_NDVI, cfg.stage_mode(cfg.ndvi_run_mode), cfg.run_tag)
    vector_dir, vector_run_id, _ = run_manager.resolve_stage_dir(
        out_dir, run_manager.STAGE_VECTOR, cfg.stage_mode(cfg.vector_run_mode), cfg.run_tag)

    # Models next: a missing or unreachable model should fail in seconds, not an hour
    # into imagery acquisition. Downloaded models land in the permanent cache.
    models = model_registry.resolve_pipeline_models(cfg, force_refresh=refresh_models)

    if cfg.needs_gee_api and gee_credentials is None:
        import gee_client

        gee_credentials, gee_project = gee_client.init_gee_and_gcs(
            cfg.gee_project_name, cfg.gee_service_account_key,
        )

    ndvi_started_at = time.time()
    sieved_ndvi_path = run_ndvi_pipeline(
        cfg, ndvi_dir, ndvi_model_path=str(models["ndvi_model"]),
        gee_credentials=gee_credentials, gee_project=gee_project,
    )
    ndvi_minutes = (time.time() - ndvi_started_at) / 60
    logger.info(f"NDVI stage finished in {ndvi_minutes:.1f} min -> {sieved_ndvi_path}")

    run_manager.write_run_info(ndvi_dir, {
        "stage": "ndvi", "run_id": ndvi_run_id, "resumed": ndvi_resumed,
        "source": cfg.ndvi_source, "crop": cfg.crop, "year": cfg.year,
        "aoi": cfg.aoi_source or cfg.aoi_path, "minutes": round((time.time() - ndvi_started_at) / 60, 1),
        "output": str(sieved_ndvi_path),
    })

    static_dir = static_run_id = None
    static_started_at = time.time()
    sieved_static_path = None
    if cfg.run_static_model:
        static_dir, static_run_id, static_resumed = run_manager.resolve_stage_dir(
            out_dir, run_manager.STAGE_STATIC, cfg.stage_mode(cfg.static_run_mode), cfg.run_tag)
        sieved_static_path = run_static_pipeline(
            cfg, static_dir, sieved_ndvi_path, static_model_path=str(models["static_model"]),
        )
        run_manager.write_run_info(static_dir, {
            "stage": "static", "run_id": static_run_id, "resumed": static_resumed,
            "source": cfg.static_source, "crop": cfg.crop, "year": cfg.year,
            "minutes": round((time.time() - static_started_at) / 60, 1),
            "output": str(sieved_static_path),
        })
    static_minutes = (time.time() - static_started_at) / 60
    if sieved_static_path:
        logger.info(f"Static stage finished in {static_minutes:.1f} min -> {sieved_static_path}")

    # Route to the static output when it ran, otherwise the NDVI-only output -- the same
    # router the three original notebooks each ended with.
    if sieved_static_path:
        source_raster = sieved_static_path
        vector_labels = cfg.static_crop_label
    else:
        source_raster = sieved_ndvi_path
        vector_labels = cfg.ndvi_crop_classes

    vector_path = postprocess.vectorize_process_and_export(
        input_raster_path=source_raster,
        boundary_shp_path=cfg.aoi_path,
        output_dir=vector_dir,
        output_basename=cfg.output_basename,
        target_labels=vector_labels,
        relabel_as=cfg.output_polygon_label,
        min_area_acres=cfg.min_polygon_area_acres,
        save_shp_zip=cfg.export_shapefile_zip,
        dissolve_polygons=cfg.dissolve_polygons,
    )

    quality = qc.assess_result(
        vector_path=vector_path,
        aoi_path=cfg.aoi_path,
        ndvi_raster=sieved_ndvi_path,
        ndvi_crop_classes=cfg.ndvi_crop_classes,
        static_raster=sieved_static_path,
        static_crop_label=cfg.static_crop_label,
        max_crop_share_pct=cfg.qc_max_crop_share_pct,
        min_crop_share_pct=cfg.qc_min_crop_share_pct,
        min_static_retention_pct=cfg.qc_min_static_retention_pct,
        max_static_retention_pct=cfg.qc_max_static_retention_pct,
        degenerate_retention_tolerance_pct=cfg.qc_degenerate_retention_tolerance_pct,
        report_path=Path(vector_dir) / "result_check.json",
    )

    total_minutes = (time.time() - started_at) / 60
    logger.info(f"Pipeline finished in {total_minutes:.1f} min -> {vector_path}")
    run_manager.write_run_info(vector_dir, {
        "stage": "vector", "run_id": vector_run_id, "crop": cfg.crop, "year": cfg.year,
        "source_raster": str(source_raster), "output": str(vector_path),
        "total_minutes": round(total_minutes, 1), "result_check": quality,
    })

    return {
        "crop": cfg.crop,
        "year": cfg.year,
        "district": cfg.district_name,
        "ndvi_source": cfg.ndvi_source,
        "static_source": cfg.static_source if cfg.run_static_model else None,
        "output_dir": str(out_dir),
        "ndvi_run": f"{run_manager.STAGE_NDVI}_run_{ndvi_run_id}",
        "static_run": f"{run_manager.STAGE_STATIC}_run_{static_run_id}" if static_run_id else None,
        "vector_run": f"{run_manager.STAGE_VECTOR}_run_{vector_run_id}",
        "sieved_ndvi_raster": str(sieved_ndvi_path),
        "sieved_static_raster": str(sieved_static_path) if sieved_static_path else None,
        "vector_output": str(vector_path) if vector_path else None,
        "ndvi_minutes": round(ndvi_minutes, 1),
        "static_minutes": round(static_minutes, 1) if sieved_static_path else None,
        "total_minutes": round(total_minutes, 1),
        "result_check": quality,
    }
