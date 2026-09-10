"""GEE-backed acquisition: AOI grid split, shapefile-to-GEE-asset ingestion, NDVI grid
export, and static composite export (automatic cloud/date selection, or a manually
supplied date / date pair).

Ported from 1_fao_model_execution_pipeline_using_gee.ipynb and de-globalised: every
function takes an explicit `PipelineConfig` instead of reading notebook globals.

Landsat 7 is deliberately never used -- its post-2003 SLC-off scan-line-corrector
failure leaves permanent diagonal no-data stripes on every scene, which is unusable for
per-pixel crop classification. Pre-Sentinel-2 years use Landsat 8 only, and the
threshold year is configurable via `cfg.gee_landsat_cutover_year`.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import ee
import geopandas as gpd
from google.oauth2 import service_account
from shapely.geometry import box
from tqdm import tqdm

from config import PipelineConfig

logger = logging.getLogger(__name__)

ACRE_TO_SQM = 4046.8564224
SQM_TO_ACRE = 1 / ACRE_TO_SQM
NDVI_SOURCE_BANDS = ["B4", "B8"]  # red, NIR -- NDVI itself is recomputed at inference time
EXPORT_CRS = "EPSG:4326"
MAX_EXPORT_PIXELS = 1e13
SCENE_CLOUD_LIMIT_PCT = 80


def init_gee_and_gcs(project_name: str, key_path: str) -> Tuple[Any, str]:
    """One-time GEE + GCS authentication. Returns (scoped_credentials, ee_project)."""
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path
    credentials = service_account.Credentials.from_service_account_file(key_path)
    scoped_credentials = credentials.with_scopes([
        "https://www.googleapis.com/auth/earthengine",
        "https://www.googleapis.com/auth/cloud-platform",
    ])
    ee_project = f"ee-{project_name}"
    ee.Initialize(
        credentials=scoped_credentials,
        project=ee_project,
        opt_url="https://earthengine-highvolume.googleapis.com",
    )
    logger.info(f"Earth Engine initialised for {ee_project}.")
    return scoped_credentials, ee_project


# ---------------------------------------------------------------------------
# AOI grid splitting -- GEE's per-export pixel/size limits require large AOIs to be
# exported grid cell by grid cell. (Static composites are exported once, whole.)
# ---------------------------------------------------------------------------

def estimate_utm_epsg(longitude: float, latitude: float) -> str:
    zone = int((longitude + 180) / 6) + 1
    return f"EPSG:{(32600 if latitude >= 0 else 32700) + zone}"


def split_aoi_into_grid(
    aoi_path: str,
    filename_suffix: str,
    grid_cell_acres: int = 500,
    save_grid_overlay: bool = False,
    output_dir: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Clips the AOI to a UTM-projected fishnet so GEE NDVI exports stay within size
    limits. The AOI may be any vector format geopandas can read (.shp, .gpkg,
    .geojson, ...); the gridded output is always a shapefile, because that is what GEE
    table ingestion accepts.

    `output_dir` is where the derived files go. Pass it whenever the AOI itself is not
    in a writable, run-specific place -- notably a GCS-cached AOI, whose cache directory
    must not accumulate pipeline artefacts. Resume-safe: returns immediately when the
    outputs already exist.
    """
    started_at = time.time()

    aoi_stem = Path(aoi_path).stem
    target_dir = Path(output_dir) if output_dir else Path(aoi_path).parent
    target_dir.mkdir(parents=True, exist_ok=True)

    gridded_aoi_path = str(target_dir / f"{aoi_stem}_{filename_suffix}.shp")
    grid_overlay_path = (
        str(target_dir / f"{aoi_stem}_grid_{grid_cell_acres}_acres.gpkg")
        if save_grid_overlay else None
    )

    if os.path.exists(gridded_aoi_path) and (not save_grid_overlay or os.path.exists(grid_overlay_path)):
        logger.info(f"[Skipped] Gridded AOI already exists: {gridded_aoi_path}")
        return {"gridded_aoi": gridded_aoi_path, "grid_overlay": grid_overlay_path}

    aoi = gpd.read_file(aoi_path)
    if aoi.empty:
        raise ValueError(f"AOI contains no features: {aoi_path}")

    # Repair before overlaying. A self-intersecting or mixed-type AOI makes the
    # intersection produce GeometryCollections, and gpd.overlay(keep_geom_type=True)
    # raises "`keep_geom_type` does not support GeometryCollection" outright.
    invalid = ~aoi.geometry.is_valid
    if invalid.any():
        logger.warning(f"Repairing {int(invalid.sum())} invalid AOI geometr(ies) before gridding.")
        aoi.loc[invalid, "geometry"] = aoi.loc[invalid, "geometry"].make_valid()
    aoi = aoi.explode(index_parts=False).reset_index(drop=True)
    aoi = aoi[aoi.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    aoi = aoi[~aoi.geometry.is_empty]
    if aoi.empty:
        raise ValueError(f"AOI has no polygon geometry after cleaning: {aoi_path}")

    min_x, min_y, max_x, max_y = aoi.total_bounds
    bbox = box(min_x, min_y, max_x, max_y)

    if aoi.crs and aoi.crs.to_epsg() != 4326:
        centroid = gpd.GeoSeries([bbox.centroid], crs=aoi.crs).to_crs(4326).iloc[0]
        longitude, latitude = centroid.x, centroid.y
    else:
        longitude, latitude = bbox.centroid.x, bbox.centroid.y

    utm_crs = estimate_utm_epsg(longitude, latitude)
    aoi_utm = aoi.to_crs(utm_crs)
    bbox_utm = gpd.GeoSeries([bbox], crs=aoi.crs).to_crs(utm_crs).iloc[0]

    cell_side_m = math.sqrt(grid_cell_acres * ACRE_TO_SQM)
    grid_min_x, grid_min_y, grid_max_x, grid_max_y = bbox_utm.bounds
    n_cols = math.ceil((grid_max_x - grid_min_x) / cell_side_m)
    n_rows = math.ceil((grid_max_y - grid_min_y) / cell_side_m)

    cells, cell_ids, next_id = [], [], 1
    for col in range(n_cols):
        for row in range(n_rows):
            x0, y0 = grid_min_x + col * cell_side_m, grid_min_y + row * cell_side_m
            cell = box(x0, y0, min(x0 + cell_side_m, grid_max_x), min(y0 + cell_side_m, grid_max_y))
            if not cell.is_empty:
                cells.append(cell)
                cell_ids.append(next_id)
                next_id += 1

    grid = gpd.GeoDataFrame({"grid_id": cell_ids}, geometry=cells, crs=utm_crs)
    try:
        gridded_aoi = gpd.overlay(aoi_utm, grid, how="intersection", keep_geom_type=True)
    except TypeError as exc:
        # Some geometry pairs still intersect into a GeometryCollection despite the
        # cleaning above; keep every type, then filter to polygons ourselves.
        logger.warning(f"Grid overlay produced mixed geometry types ({exc}); filtering manually.")
        gridded_aoi = gpd.overlay(aoi_utm, grid, how="intersection", keep_geom_type=False)
        gridded_aoi = gridded_aoi.explode(index_parts=False).reset_index(drop=True)
        gridded_aoi = gridded_aoi[gridded_aoi.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]

    gridded_aoi = gridded_aoi[~gridded_aoi.geometry.is_empty].reset_index(drop=True)
    if gridded_aoi.empty:
        raise ValueError(
            f"Gridding {aoi_path} produced no cells -- the AOI and the grid do not intersect."
        )
    gridded_aoi["f_Id"] = range(1, len(gridded_aoi) + 1)

    # Keep only what GEE needs. Carrying the source AOI's own attributes through would
    # silently truncate any name over 10 characters on the shapefile write (and two
    # names truncating to the same 10 characters is a hard failure), for columns the
    # pipeline never reads.
    gridded_aoi = gridded_aoi[["grid_id", "f_Id", "geometry"]]
    gridded_aoi.to_file(gridded_aoi_path)

    used_cell_ids = gridded_aoi["grid_id"].unique()
    if save_grid_overlay:
        grid[grid["grid_id"].isin(used_cell_ids)].to_file(
            grid_overlay_path, layer="grid_intersecting", driver="GPKG",
        )

    logger.info(
        f"AOI split into {len(used_cell_ids)}/{len(grid)} grid cells "
        f"({grid_cell_acres} acres each) in {time.time() - started_at:.1f}s"
    )
    return {"gridded_aoi": gridded_aoi_path, "grid_overlay": grid_overlay_path}


# ---------------------------------------------------------------------------
# GCS shapefile upload + GEE asset ingestion (NDVI grid path only)
# ---------------------------------------------------------------------------

def upload_shapefile_to_gcs(
    shapefile_path: str,
    bucket_name: str,
    base_folder: str,
    target_folder: Optional[str],
    credentials: Any,
    gcp_project: str,
) -> str:
    """Uploads every shapefile sidecar via atomic (non-chunked) PUT requests."""
    from google.api_core import retry
    from google.cloud import storage

    if not os.path.exists(shapefile_path):
        raise FileNotFoundError(f"Shapefile not found: {shapefile_path}")

    shapefile_dir = os.path.dirname(shapefile_path)
    shapefile_stem = os.path.splitext(os.path.basename(shapefile_path))[0]
    target_folder = target_folder or shapefile_stem

    gcs_folder = f"{base_folder}/{target_folder}"
    gcs_shapefile_path = f"{gcs_folder}/{shapefile_stem}.shp"
    gcs_uri = f"gs://{bucket_name}/{gcs_shapefile_path}"

    bucket = storage.Client(credentials=credentials, project=gcp_project).bucket(bucket_name)
    if bucket.blob(gcs_shapefile_path).exists():
        logger.info(f"Shapefile already on GCS: {gcs_uri}")
        return gcs_uri

    sidecar_extensions = [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"]
    files_to_upload = [
        os.path.join(shapefile_dir, shapefile_stem + extension)
        for extension in sidecar_extensions
        if os.path.exists(os.path.join(shapefile_dir, shapefile_stem + extension))
    ]
    if not files_to_upload:
        raise RuntimeError(f"No shapefile components found alongside {shapefile_path}")

    for file_path in tqdm(files_to_upload, desc="Uploading shapefile components"):
        blob = bucket.blob(f"{gcs_folder}/{os.path.basename(file_path)}")
        blob.chunk_size = None  # single atomic upload; resumable uploads hang on small files
        blob.upload_from_filename(
            file_path, timeout=(15, 120),
            retry=retry.Retry(initial=1.0, maximum=10.0, multiplier=2.0),
        )

    logger.info(f"Uploaded shapefile to {gcs_uri}")
    return gcs_uri


def ingest_shapefile_to_gee_asset(
    gcs_shapefile_uri: str,
    asset_folder: str,
    asset_name: Optional[str] = None,
    overwrite: bool = False,
    timeout_seconds: int = 1500,
) -> str:
    """Ingests a GCS shapefile into a GEE FeatureCollection asset, polling until done."""
    default_name = os.path.splitext(os.path.basename(gcs_shapefile_uri))[0]
    final_name = (asset_name or default_name).replace(" ", "_")
    asset_id = f"{asset_folder}/{final_name}"

    try:
        ee.data.getAsset(asset_id)
        if overwrite:
            ee.data.deleteAsset(asset_id)
        else:
            logger.info(f"GEE asset already exists, skipping ingestion: {asset_id}")
            return asset_id
    except ee.EEException:
        pass

    manifest = {"name": asset_id, "sources": [{"uris": [gcs_shapefile_uri], "charset": "UTF-8"}]}
    started_at = time.time()
    try:
        request_id = ee.data.newTaskId()[0]
        response = ee.data.startTableIngestion(request_id, manifest, overwrite)
        task_id = response.get("id", request_id)
        logger.info(f"Ingesting {gcs_shapefile_uri} -> {asset_id} (task {task_id})")

        while True:
            if time.time() - started_at > timeout_seconds:
                raise TimeoutError(f"GEE ingestion timed out after {timeout_seconds}s.")

            # A very fast ingestion can finish before the task list reports it.
            try:
                if ee.data.getAsset(asset_id):
                    break
            except ee.EEException:
                pass

            statuses = ee.data.getTaskStatus([task_id])
            state = statuses[0].get("state", "UNKNOWN") if statuses else "UNKNOWN"
            if state in ("COMPLETED", "SUCCEEDED"):
                break
            if state in ("FAILED", "CANCELLED"):
                raise RuntimeError(f"GEE ingestion {state}: {statuses[0].get('error_message', '')}")
            time.sleep(5)
    except Exception as exc:
        raise RuntimeError(f"GEE ingestion failed for {gcs_shapefile_uri}: {exc}")

    return asset_id


# ---------------------------------------------------------------------------
# Harmonized collection: Sentinel-2, or Landsat 8 only for pre-Sentinel-2 years
# ---------------------------------------------------------------------------

def homogenize_landsat8(image: ee.Image) -> ee.Image:
    """Rescales Landsat 8 SR bands to Sentinel-2-like uint16 scaling, renames them to
    S2 nomenclature, and synthesises a red-edge B5 as (B4+B8)/2, so the static
    classifier sees the same six bands in the same order regardless of sensor."""
    scaled = image.select(["SR_B2", "SR_B3", "SR_B4", "SR_B5"]) \
                  .multiply(0.0000275).add(-0.2) \
                  .multiply(10000).rename(["B2", "B3", "B4", "B8"])
    red_edge = scaled.select("B4").add(scaled.select("B8")).divide(2).rename("B5")
    ndvi = scaled.normalizedDifference(["B8", "B4"]).multiply(10000).rename("NDVI")

    harmonized = ee.Image.cat([
        scaled.select(["B2", "B3", "B4"]), red_edge, scaled.select("B8"), ndvi,
    ]).max(0).uint16()
    harmonized = harmonized.set("CLOUD_COVER_STD", image.get("CLOUD_COVER"))
    return harmonized.copyProperties(image, image.propertyNames())


def build_harmonized_collection(
    geometry: ee.Geometry, start_date: str, end_date: str, sensor_mode: str, mask_clouds: bool = True,
) -> ee.ImageCollection:
    """Returns a collection that always behaves like Sentinel-2, whatever the sensor."""
    if sensor_mode == "SENTINEL":
        collection = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED") \
                       .filterBounds(geometry).filterDate(start_date, end_date)

        def prepare_sentinel(image):
            image = image.set("CLOUD_COVER_STD", image.get("CLOUDY_PIXEL_PERCENTAGE"))
            if mask_clouds:
                scl = image.select("SCL")
                # Drop cloud shadow (3), cloud medium/high probability (8/9), cirrus (10).
                image = image.updateMask(scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)))
            ndvi = image.normalizedDifference(["B8", "B4"]).multiply(10000).rename("NDVI")
            return image.addBands(ndvi).uint16()

        return collection.map(prepare_sentinel)

    # Landsat 8 only -- LANDSAT/LE07 is excluded on purpose (SLC-off striping).
    landsat8 = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2") \
                 .filterBounds(geometry).filterDate(start_date, end_date)

    def mask_landsat_qa(image):
        qa = image.select("QA_PIXEL")
        return image.updateMask(qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 4).eq(0)))

    if mask_clouds:
        landsat8 = landsat8.map(mask_landsat_qa)
    return landsat8.map(homogenize_landsat8)


# ---------------------------------------------------------------------------
# Static composite selection
# ---------------------------------------------------------------------------

def score_by_cloud_and_distance(image: ee.Image, anchor_timestamp: ee.Number) -> ee.Image:
    """Lower is better: days away from the anchor scene plus a cloud-cover penalty."""
    days_from_anchor = ee.Number(image.get("system:time_start")) \
                         .subtract(anchor_timestamp).abs().divide(1000 * 60 * 60 * 24)
    cloud_penalty = ee.Number(image.get("CLOUD_COVER_STD")).divide(5.0)
    return image.set("mosaic_penalty", days_from_anchor.add(cloud_penalty))


def static_composite_filename(
    asset_base: str, manual_mode: bool, composite_type: str,
    single_date: Optional[str], top_date: Optional[str], bottom_date: Optional[str],
    auto_dates: str = "",
) -> str:
    if manual_mode:
        label = single_date if composite_type == "single" else f"{top_date}_over_{bottom_date}"
        return f"static_cleanup_{asset_base}_manual_{label}"
    return f"static_cleanup_{asset_base}_auto_{auto_dates}"


def pick_auto_composite_dates(
    geometry: ee.Geometry, sensor_mode: str, window_start: str, window_end: str,
) -> str:
    """Returns up to two best-scoring acquisition dates, joined by '_', for labelling."""
    collection = build_harmonized_collection(geometry, window_start, window_end, sensor_mode, mask_clouds=False) \
        .filter(ee.Filter.lt("CLOUD_COVER_STD", SCENE_CLOUD_LIMIT_PCT))
    try:
        anchor = collection.sort("CLOUD_COVER_STD", True).first()
        anchor_timestamp = ee.Number(anchor.get("system:time_start"))
        ranked = collection.map(lambda image: score_by_cloud_and_distance(image, anchor_timestamp))
        dates = (ranked.sort("mosaic_penalty", True)
                 .map(lambda image: ee.Feature(None, {"date": image.date().format("YYYY-MM-dd")}))
                 .aggregate_array("date").distinct().slice(0, 2).getInfo())
        return "_".join(dates) if dates else "no_valid_images"
    except Exception as exc:
        logger.warning(f"Could not resolve automatic composite dates: {exc}")
        return f"{window_start}_to_{window_end}"


def build_static_composite(
    geometry: ee.Geometry, window_start: str, window_end: str, bands: List[str], sensor_mode: str,
    manual_mode: bool = False, composite_type: str = "single",
    single_date: Optional[str] = None, top_date: Optional[str] = None, bottom_date: Optional[str] = None,
) -> ee.Image:
    def one_day_mosaic(date_text: str) -> ee.Image:
        day = ee.Date(date_text)
        collection = build_harmonized_collection(
            geometry, day.format("YYYY-MM-dd"), day.advance(1, "day").format("YYYY-MM-dd"),
            sensor_mode, mask_clouds=True,
        )
        # Check here, client-side. An empty collection mosaics to a band-less image whose
        # .select() only fails once GEE has scheduled the export, surfacing minutes later
        # as "1 export task(s) failed" with the real reason in a separate log line. The
        # usual cause is a date picked from Sentinel-2 availability on a pre-cutover year,
        # where the static side silently switches to Landsat 8.
        if collection.size().getInfo() == 0:
            raise ValueError(
                f"No {sensor_mode} acquisition on {date_text} over this AOI. "
                f"(Years before cfg.gee_landsat_cutover_year use Landsat 8, whose overpass "
                f"dates differ from Sentinel-2's.)"
            )
        return collection.mosaic()

    if manual_mode:
        if composite_type == "single" and single_date:
            composite = one_day_mosaic(single_date)
        elif composite_type == "mosaic" and top_date and bottom_date:
            # Later images win in ee.mosaic(), so the "top" date goes last.
            composite = ee.ImageCollection([one_day_mosaic(bottom_date), one_day_mosaic(top_date)]).mosaic()
        else:
            raise ValueError("Manual composite needs single_date, or both top_date and bottom_date.")
    else:
        collection = build_harmonized_collection(geometry, window_start, window_end, sensor_mode, True) \
            .filter(ee.Filter.lt("CLOUD_COVER_STD", SCENE_CLOUD_LIMIT_PCT))
        anchor_timestamp = ee.Number(collection.sort("CLOUD_COVER_STD", True).first().get("system:time_start"))
        composite = collection.map(lambda image: score_by_cloud_and_distance(image, anchor_timestamp)) \
                              .sort("mosaic_penalty", False).mosaic()

    return composite.select(bands).uint16()


# ---------------------------------------------------------------------------
# NDVI time-series export
# ---------------------------------------------------------------------------

def iter_date_windows(start_date: str, end_date: str, step_days: int) -> Generator[Tuple[str, str], None, None]:
    current = datetime.strptime(start_date, "%Y-%m-%d")
    final = datetime.strptime(end_date, "%Y-%m-%d")
    while current < final:
        next_edge = min(current + timedelta(days=step_days), final)
        yield current.strftime("%Y-%m-%d"), next_edge.strftime("%Y-%m-%d")
        current = next_edge


def build_window_composite(
    geometry: ee.Geometry, start_date: str, end_date: str, sensor_mode: str,
) -> ee.Image:
    """Cloud-masked median composite of the red/NIR bands for one compositing window."""
    collection = build_harmonized_collection(geometry, start_date, end_date, sensor_mode, mask_clouds=True) \
        .filter(ee.Filter.lt("CLOUD_COVER_STD", SCENE_CLOUD_LIMIT_PCT))
    median = collection.select(NDVI_SOURCE_BANDS).median().uint16()
    # A fully-masked placeholder keeps the band count constant when a window is empty.
    placeholder = ee.Image.constant([0] * len(NDVI_SOURCE_BANDS)) \
                          .rename(NDVI_SOURCE_BANDS).uint16().updateMask(0)
    return ee.ImageCollection([placeholder, median]).mosaic()


def find_existing_exports(
    bucket_name: str, folder_prefix: str, grid_ids: List[int], key_path: str,
) -> Tuple[List[int], Optional[str]]:
    """Returns (grid ids still missing from GCS, newest manifest URI)."""
    import gcs_io

    bucket = gcs_io.gcs_client(key_path).bucket(bucket_name)
    existing = {blob.name for blob in bucket.list_blobs(prefix=folder_prefix)}

    missing = [
        grid_id for grid_id in grid_ids
        if f"{folder_prefix}/ndvi_stack_grid_{grid_id}.tif" not in existing
        and not any(
            re.match(rf"^{re.escape(folder_prefix)}/ndvi_stack_grid_{grid_id}\d{{10}}-\d{{10}}\.tif$", name)
            for name in existing
        )
    ]
    manifests = sorted(n for n in existing if "master_manifest" in n and n.endswith(".csv"))
    latest_manifest = f"gs://{bucket_name}/{manifests[-1]}" if manifests else None
    return missing, latest_manifest


def submit_grid_export(
    grid_id: int, features: ee.FeatureCollection, asset_base: str, timestamp: str,
    cfg: PipelineConfig, sensor_mode: str, priority: int = 99,
) -> Optional[Dict[str, Any]]:
    """Submits one grid cell's full NDVI band-stack as a GEE export task."""
    max_attempts, base_delay_s = 5, 2
    for attempt in range(max_attempts):
        try:
            geometry = features.filter(ee.Filter.eq("grid_id", grid_id)).geometry().bounds()
            window_images, band_names = [], []
            for window_start, window_end in iter_date_windows(
                cfg.ndvi_series_start, cfg.ndvi_series_end, cfg.composite_step_days
            ):
                composite = build_window_composite(geometry, window_start, window_end, sensor_mode) \
                    .select(NDVI_SOURCE_BANDS)
                window_band_names = [f"{band}_{window_end.replace('-', '_')}" for band in NDVI_SOURCE_BANDS]
                window_images.append(composite.rename(window_band_names))
                band_names.extend(window_band_names)

            stack = ee.ImageCollection(window_images).toBands().rename(band_names).uint16()
            blob_prefix = f"{cfg.gcs_base_folder}/{asset_base}/ndvi_stack_grid_{grid_id}"

            task = ee.batch.Export.image.toCloudStorage(
                image=stack,
                description=f"Export_{sensor_mode}_{asset_base}_grid_{grid_id}_{timestamp}",
                bucket=cfg.gcs_bucket, fileNamePrefix=blob_prefix, region=geometry,
                scale=cfg.gee_resolution_m, crs=EXPORT_CRS, maxPixels=MAX_EXPORT_PIXELS, priority=priority,
            )
            task.start()
            return {
                "grid_id": grid_id,
                "gcs_path": f"gs://{cfg.gcs_bucket}/{blob_prefix}.tif",
                "task_id": task.id,
                "status": "SUBMITTED",
            }
        except ee.ee_exception.EEException as exc:
            if any(term in str(exc).lower() for term in ("quota", "too many requests", "rate limit")):
                time.sleep(base_delay_s * (2 ** attempt))
            else:
                raise
    logger.error(f"Grid {grid_id}: export submission gave up after {max_attempts} attempts.")
    return None


def export_static_composite(
    features: ee.FeatureCollection, asset_base: str, timestamp: str, bands: List[str],
    output_filename: str, cfg: PipelineConfig, sensor_mode: str, priority: int = 100,
    manual_mode: bool = False, composite_type: str = "single",
    single_date: Optional[str] = None, top_date: Optional[str] = None, bottom_date: Optional[str] = None,
) -> dict:
    """Submits the static composite as a single whole-AOI export task."""
    geometry = features.geometry().bounds()
    composite = build_static_composite(
        geometry, cfg.static_window_start, cfg.static_window_end, bands, sensor_mode,
        manual_mode=manual_mode, composite_type=composite_type,
        single_date=single_date, top_date=top_date, bottom_date=bottom_date,
    )
    blob_prefix = f"{cfg.gcs_base_folder}/{asset_base}/{output_filename}"

    task = ee.batch.Export.image.toCloudStorage(
        image=composite,
        description=f"Export_{sensor_mode}_{asset_base}_static_{timestamp}",
        bucket=cfg.gcs_bucket, fileNamePrefix=blob_prefix, region=geometry,
        scale=cfg.gee_resolution_m, crs=EXPORT_CRS, maxPixels=MAX_EXPORT_PIXELS, priority=priority,
    )
    task.start()
    return {
        "type": "static_composite",
        "gcs_path": f"gs://{cfg.gcs_bucket}/{blob_prefix}.tif",
        "task_id": task.id,
        "status": "SUBMITTED",
    }


def wait_for_export_tasks(task_ids: List[str], timeout_hours: float = 48.0) -> None:
    """Polls GEE until every task finishes; raises if any failed or the wait timed out."""
    if not task_ids:
        return

    logger.info(f"Monitoring {len(task_ids)} GEE export task(s)...")
    started_at = time.time()
    pending = set(task_ids)
    failures: List[str] = []
    consecutive_errors = 0

    while pending:
        elapsed_s = time.time() - started_at
        if elapsed_s > timeout_hours * 3600:
            raise TimeoutError(f"GEE export tasks exceeded {timeout_hours}h; {len(pending)} still pending.")

        statuses = []
        pending_list = list(pending)
        for offset in range(0, len(pending_list), 50):  # GEE caps status queries per call
            try:
                if offset:
                    time.sleep(1)
                statuses.extend(ee.data.getTaskStatus(pending_list[offset:offset + 50]))
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                logger.warning(f"Task status query failed (attempt {consecutive_errors}): {exc}")
                time.sleep(min(10 * (2 ** consecutive_errors), 300))

        for status in statuses:
            if status["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                pending.discard(status["id"])
                if status["state"] == "FAILED":
                    failures.append(f"Task {status['id']} FAILED: {status.get('error_message', 'unknown error')}")

        if not pending:
            break
        time.sleep(20 if elapsed_s < 3600 else 60 if elapsed_s < 14400 else 90)

    if failures:
        for failure in failures:
            logger.error(failure)
        raise RuntimeError(f"{len(failures)} GEE export task(s) failed.")
    logger.info("All GEE export tasks completed.")


def export_ndvi_grid_stacks(
    gee_asset_id: str, cfg: PipelineConfig, sensor_mode: str, priority: int = 99,
) -> List[str]:
    """Exports one NDVI band-stack per grid cell to GCS and returns their GCS URIs.

    Idempotent: cells already present in the bucket are skipped, so a re-run after a
    partial failure only submits what is actually missing.
    """
    features = ee.FeatureCollection(gee_asset_id)
    asset_base = gee_asset_id.split("/")[-1]
    folder_prefix = f"{cfg.gcs_base_folder}/{asset_base}"

    grid_ids = features.aggregate_array("grid_id").distinct().getInfo()
    missing_grid_ids, _ = find_existing_exports(
        cfg.gcs_bucket, folder_prefix, grid_ids, cfg.gee_service_account_key,
    )

    all_uris = [f"gs://{cfg.gcs_bucket}/{folder_prefix}/ndvi_stack_grid_{gid}.tif" for gid in grid_ids]

    if not missing_grid_ids:
        logger.info(f"All {len(grid_ids)} NDVI grid stacks already present on GCS.")
        return all_uris

    logger.info(f"Submitting {len(missing_grid_ids)} of {len(grid_ids)} NDVI grid exports...")
    timestamp = (datetime.now(timezone.utc) + timedelta(hours=5)).strftime("%Y%m%d_%H%M%S")
    submitted_task_ids = []

    with ThreadPoolExecutor(max_workers=cfg.gee_export_submit_workers) as pool:
        futures = {
            pool.submit(submit_grid_export, grid_id, features, asset_base, timestamp, cfg, sensor_mode, priority): grid_id
            for grid_id in missing_grid_ids
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="Submitting NDVI grid exports"):
            result = future.result()
            if result:
                submitted_task_ids.append(result["task_id"])

    if cfg.gee_wait_for_exports:
        wait_for_export_tasks(submitted_task_ids)
    else:
        logger.warning(
            f"{len(submitted_task_ids)} export(s) submitted with gee_wait_for_exports=False; "
            "downloads will fail until they finish."
        )

    return all_uris
