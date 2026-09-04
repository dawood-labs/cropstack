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


def _check_static_coverage(cfg: PipelineConfig, coverage_pct) -> None:
    """A cloud-free-looking date can still cover a fraction of the AOI.

    farmdar's selector reports how much of the AOI its chosen dates actually make usable.
    Nothing downstream reads it, so a 17%-usable image would be classified as if it were
    clear ground and reported without a word. This surfaces that.
    """
    if coverage_pct is None or cfg.stac_static_min_coverage_pct is None:
        return
    if coverage_pct >= cfg.stac_static_min_coverage_pct:
        return

    message = (
        f"Static imagery covers only {coverage_pct:.1f}% of the AOI "
        f"(minimum {cfg.stac_static_min_coverage_pct:.1f}%). The remainder is cloud or "
        f"outside the scene footprint, and the classifier cannot tell the difference -- "
        f"results over that area are not trustworthy."
    )
    if cfg.stac_static_on_low_coverage == "error":
        raise RuntimeError(message)
    logger.warning("LOW STATIC COVERAGE: " + message)


def _score_window(cfg: PipelineConfig, start: str, end: str, label: str) -> Optional[dict]:
    """Scores one window without downloading imagery: farmdar's selector reads metadata
    and the coarse SCL overview only (~1 MB per window against ~2.5 GB for a real
    acquisition). Returns None when the window has no usable acquisition at all."""
    from farmdar.sentinel import select_static_dates

    try:
        selection = select_static_dates(cfg.aoi_path, start, end, **dict(cfg.stac_static_selection))
    except Exception as exc:
        logger.warning(f"{label}: could not be scored ({exc}).")
        return None

    dates = selection.get("dates") or []
    if not dates:
        logger.info(f"{label}: no usable acquisition.")
        return None

    selection["window"] = (start, end)
    selection["label"] = label
    return selection


def _expanded_windows(cfg: PipelineConfig, windows) -> list:
    """Last resort when no configured window has any imagery: widens the best window
    symmetrically, `static_window_expansion_days` at a time. This only ever runs when the
    alternative is failing the run outright, and each step is logged, because every day
    of expansion moves the acquisition further from the phenology the windows encode."""
    if not windows or cfg.static_window_expansion_days <= 0:
        return []

    anchor_start, anchor_end = windows[0]
    step = timedelta(days=cfg.static_window_expansion_days)
    lower = datetime.strptime(anchor_start, "%Y-%m-%d")
    upper = datetime.strptime(anchor_end, "%Y-%m-%d")

    expanded = []
    for _ in range(cfg.static_window_max_expansions):
        lower -= step
        upper += step
        expanded.append((lower.strftime("%Y-%m-%d"), upper.strftime("%Y-%m-%d")))
    return expanded


def select_dates_by_priority(cfg: PipelineConfig) -> Tuple[Optional[list], dict, str]:
    """Scores the crop's acquisition windows and returns the dates from the one to use.

    A single wide window lets the selector pick any date in it, and a scene that looks
    perfect by `eo:cloud_cover` can still be a swath edge covering a fraction of the AOI,
    or hazy enough to shift the DN values the static model keys on. Narrow, ordered
    windows encode the phenology instead: earlier windows are agronomically better dates.

    Every window is scored before one is chosen, and all the scores are logged. Taking
    the first window merely past the coverage floor hides how much the answer depends on
    the date -- one crop's three windows have produced acreages 8.9x apart, with the
    first accepted at 99.1% while a later one sat at 100%. So: among the windows that
    clear the floor, the earliest (agronomically best) wins unless a later one is better
    by more than `static_window_preference_margin_pct`, in which case coverage decides.

    `cfg.static_window_start_at` skips ahead, for re-running a district whose result from
    the leading window looked wrong.

    Returns (dates, selection, description). `dates` is None when nothing is usable.
    `selection["window_scores"]` carries every window's score for the run record.
    """
    windows = cfg.resolved_static_windows()
    if not windows:
        return None, {}, "no priority windows configured"

    skip = max(0, int(cfg.static_window_start_at) - 1)
    if skip:
        if skip >= len(windows):
            raise ValueError(
                f"static_window_start_at={cfg.static_window_start_at} but {cfg.crop} has "
                f"only {len(windows)} window(s)."
            )
        logger.warning(f"Skipping the first {skip} window(s) at the operator's request.")
        windows = windows[skip:]

    floor = cfg.stac_static_min_coverage_pct or 0.0
    scored = []
    for position, (start, end) in enumerate(windows, start=skip + 1):
        label = f"window {position} ({start} to {end})"
        selection = _score_window(cfg, start, end, label)
        if selection:
            scored.append(selection)

    if not scored:
        fallback = _expanded_windows(cfg, windows)
        for position, (start, end) in enumerate(fallback, start=1):
            label = f"expanded window +{position * cfg.static_window_expansion_days}d ({start} to {end})"
            logger.warning(
                f"No configured window had usable imagery; widening the leading window "
                f"to {start}..{end}. This date is outside the phenology the windows encode."
            )
            selection = _score_window(cfg, start, end, label)
            if selection:
                scored.append(selection)
                break

    if not scored:
        return None, {}, "no window produced a usable acquisition"

    def coverage_of(selection) -> float:
        value = selection.get("coverage_pct")
        return -1.0 if value is None else float(value)

    summary = [
        {"label": s["label"], "window": s["window"], "dates": s.get("dates"),
         "coverage_pct": s.get("coverage_pct")}
        for s in scored
    ]
    logger.info("Window scores (best-first by preference):")
    for row in summary:
        coverage = row["coverage_pct"]
        logger.info(f"  {row['label']}: {row['dates']} -> "
                    + (f"{coverage:.1f}% of AOI usable" if coverage is not None else "coverage unknown"))

    spread = [c for c in (coverage_of(s) for s in scored) if c >= 0]
    if spread and (max(spread) - min(spread)) > 0:
        logger.info(f"Coverage spread across windows: {min(spread):.1f}%-{max(spread):.1f}%. "
                    "The chosen date, not just the code, determines the acreage reported.")

    eligible = [s for s in scored if coverage_of(s) >= floor]
    if eligible:
        best_coverage = max(coverage_of(s) for s in eligible)
        margin = cfg.static_window_preference_margin_pct
        chosen = next(s for s in eligible if coverage_of(s) >= best_coverage - margin)
        if coverage_of(chosen) < best_coverage:
            logger.info(
                f"Preferring {chosen['label']} at {coverage_of(chosen):.1f}% over the "
                f"highest-coverage {best_coverage:.1f}%: within the {margin:.0f}-point "
                "margin, and it is the agronomically better date."
            )
        else:
            logger.info(f"Chose {chosen['label']} at {coverage_of(chosen):.1f}% "
                        f"(floor {floor:.0f}%).")
    else:
        chosen = max(scored, key=coverage_of)
        logger.warning(
            f"No window reached the {floor:.0f}% coverage floor. Falling back to the best "
            f"available: {chosen['label']} at {chosen.get('coverage_pct')}% -- the result "
            "rests on partly cloudy or partly covered imagery."
        )
        chosen = dict(chosen)
        chosen["label"] += " (below floor)"

    chosen = dict(chosen)
    chosen["window_scores"] = summary
    return chosen.get("dates"), chosen, chosen["label"]


def _acquire_static_from_stac(cfg: PipelineConfig, staging_dir: Path) -> Tuple[str, str]:
    """Returns (static image path, date suffix). In 'auto' mode the cloud-aware
    selector inside farmdar.sentinel picks the dates; in 'manual' mode the configured
    dates are fetched as-is."""
    from farmdar.sentinel import fetch_sentinel_static_imagery  # never modified, only called
    # from sentinel import fetch_sentinel_static_imagery

    priority_label = None
    priority_selection: dict = {}
    if cfg.stac_static_mode == "manual":
        requested_dates = cfg.stac_static_dates
        selection_kwargs = {}
    elif cfg.resolved_static_windows():
        # Score the crop's phenological windows and fetch only the winner.
        requested_dates, priority_selection, priority_label = select_dates_by_priority(cfg)
        selection_kwargs = {}
        if requested_dates is None:
            raise RuntimeError(
                f"No usable static imagery in any configured window for {cfg.crop} "
                f"{cfg.year} ({priority_label})."
            )
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
    logger.info(f"STAC static dates ({cfg.stac_static_mode} mode): {selected_dates}"
                + (f" via {priority_label}" if priority_label else ""))
    if priority_label:
        # The winning window was scored before download; hold its coverage to the same
        # floor the single-window path enforces.
        _check_static_coverage(cfg, priority_selection.get("coverage_pct"))
    elif cfg.stac_static_mode == "auto":
        selection = result.get("selection", {}) or {}
        logger.info(
            f"Date selection: anchor={selection.get('anchor')}, "
            f"AOI coverage={selection.get('coverage_pct')}%, metric={selection.get('cloud_metric')}"
        )
        _check_static_coverage(cfg, selection.get("coverage_pct"))
    else:
        # The first manual date is the anchor: it is layered on top AND is the
        # radiometric reference the other layers are matched to, so a partial or
        # swath-edge scene in that position degrades the whole composite.
        logger.warning(
            f"Manual static dates: {selected_dates[0] if selected_dates else '?'} is the ANCHOR "
            "(layered on top and used as the radiometric reference). Put the date with the "
            "best AOI coverage first -- chronological order is not automatically correct."
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

    # A resumed static run whose config now selects different dates would leave two
    # unrelated date folders side by side, and which one downstream reads depends only on
    # the current config's date suffix. Say so rather than let it pass unnoticed.
    sibling_dates = [
        child.name for child in out_dir.iterdir()
        if child.is_dir() and child.name != date_suffix and not child.name.startswith(("static_staging", "final_output"))
    ] if out_dir.exists() else []
    if sibling_dates:
        logger.warning(
            f"This static run folder already holds output for {sibling_dates} and is now "
            f"adding {date_suffix}. The folder no longer identifies one result -- use "
            f"static_run_mode='new' when the static dates change."
        )

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
            memory_fraction=cfg.static_memory_fraction,
            model_memory_expansion=cfg.static_model_memory_expansion,
        )

    if cfg.delete_raw_static_tiles:
        shutil.rmtree(staging_dir, ignore_errors=True)
        crop_mask_path.unlink(missing_ok=True)
    else:
        logger.info(f"Raw static tiles kept at: {staging_dir} (crop mask: {crop_mask_path})")

    return postprocess.apply_strict_directional_sieve(
        input_raster_path=str(classified_path),
        target_classes=[cfg.static_crop_label],
        min_pixel_size=cfg.static_sieve_min_pixels,
        connectivity=4,
        # None, not the background label: the static raster has no absent pixels, and
        # naming a real class as nodata masks it out of the sieve entirely, turning the
        # whole step into a silent no-op.
        nodata_val=None,
    )
