"""Static-image (single-acquisition) XGBoost classification, restricted to the pixels
the NDVI/RF pipeline already flagged as the crop.

Ported from notebook 3's multiprocess implementation -- the most scalable of the three
near-identical versions across the original notebooks -- with three efficiency changes:

* worker processes cache their open raster handles instead of re-opening the image and
  mask for every window;
* the AOI mask is built from only the geometries that intersect each window (spatial
  index), and windows with nothing to classify skip rasterisation entirely;
* the output profile forces GTiff, because the input may be a `.vrt` whose driver would
  otherwise be inherited and make the output unwritable.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from shapely.geometry import box
from tqdm import tqdm

logger = logging.getLogger(__name__)

# Per-process caches: workers are reused across windows, so paying the model load and
# raster open cost once per process (rather than once per window) is a large saving.
_MODEL_CACHE: Dict[str, Any] = {}
_DATASET_CACHE: Dict[str, Any] = {}


def get_static_model(model_path: str) -> Any:
    model = _MODEL_CACHE.get(model_path)
    if model is None:
        # Imported lazily so an NDVI-only run needs no xgboost installed.
        import xgboost as xgb

        model = xgb.XGBClassifier()
        model.load_model(model_path)
        # CPU-only, single-threaded per worker: the process pool supplies the
        # parallelism, and one GPU context cannot be shared across pool workers.
        model.set_params(n_jobs=1, device="cpu")
        _MODEL_CACHE[model_path] = model
    return model


def available_memory_bytes() -> Optional[int]:
    """Free RAM, or None when it cannot be determined on this platform."""
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        try:  # Linux without psutil
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            return None


#: Working copies of a window a worker holds at once: the raw read, the float view the
#: model needs, the crop mask, and the prediction buffer. Measured against the Kasur run
#: rather than assumed -- see `window_working_bytes`.
WINDOW_COPIES_IN_FLIGHT = 4


def window_working_bytes(chunk_size: int, band_count: int, dtype_size: int) -> int:
    """Bytes one worker needs for one window.

    This, not the model, is what a district run actually spends. Kasur peaked at 8.8 GiB
    on a **648 KB** wheat model: at chunk_size 2048 with 6 bands each window is
    2048 x 2048 x 6 x 2 = 48 MiB raw, and a worker holds several working copies of it.
    Sizing the pool from the model alone read that as "memory is free, use every core".
    """
    return max(1, chunk_size) ** 2 * max(1, band_count) * max(1, dtype_size) * WINDOW_COPIES_IN_FLIGHT


def resolve_worker_count(
    requested: Optional[int],
    window_count: int,
    model_path: str,
    memory_fraction: float = 0.5,
    model_memory_expansion: float = 12.0,
    per_worker_window_bytes: int = 0,
    memory_budget_bytes: Optional[int] = None,
) -> int:
    """Chooses a pool size bounded by cores, by work, and by memory.

    A worker's resident cost is the sum of two independent terms, and sizing from either
    one alone gets a real case wrong:

    * **the model** -- every worker holds its own copy, and a gradient-boosted model can
      expand roughly an order of magnitude from its JSON (one 563 MB model measured at
      5.2 GiB resident), so a pool sized purely from `cpu_count()` OOMs on a big machine;
    * **the window** -- `chunk_size^2 x bands x dtype`, several copies in flight. Kasur's
      wheat model is 648 KB, so the model term said "use every core" while the windows
      were the entire 8.8 GiB.

    `memory_budget_bytes` lets a caller reserve a share of RAM -- the district planner
    uses it so several districts running at once do not each size against the whole box.
    """
    ceiling = requested if requested else max(1, multiprocessing.cpu_count() - 1)
    reasons = [f"requested={ceiling}"]

    if window_count > 0 and window_count < ceiling:
        ceiling = window_count
        reasons.append(f"windows={window_count}")

    try:
        model_bytes = os.path.getsize(model_path) * model_memory_expansion
    except OSError:
        model_bytes = 0  # cannot stat the model; the window term still applies

    per_worker_bytes = model_bytes + max(0, per_worker_window_bytes)
    available = memory_budget_bytes if memory_budget_bytes is not None else available_memory_bytes()
    if available and per_worker_bytes > 0:
        budget = int(available * memory_fraction)
        memory_cap = max(1, int(budget // per_worker_bytes))
        if memory_cap < ceiling:
            reasons.append(
                f"memory={memory_cap} "
                f"({available / 2**30:.1f} GiB x {memory_fraction:g} / "
                f"{per_worker_bytes / 2**30:.2f} GiB per worker "
                f"= {model_bytes / 2**30:.2f} model + "
                f"{per_worker_window_bytes / 2**30:.2f} window)"
            )
            ceiling = memory_cap

    if len(reasons) > 1:
        logger.info(f"Static worker pool = {ceiling}  [{'; '.join(reasons)}]")
    return max(1, ceiling)


def _open_cached(path: str):
    dataset = _DATASET_CACHE.get(path)
    if dataset is None or dataset.closed:
        dataset = rasterio.open(path)
        _DATASET_CACHE[path] = dataset
    return dataset


def configure_model_hardware(model: Any) -> Any:
    """Tries CUDA, falls back to CPU (all cores). Only meaningful for single-process
    use; `classify_static_image` always runs CPU-only workers (see `get_static_model`)."""
    probe = np.zeros((1, 6), dtype=np.float32)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            model.set_params(device="cuda")
            model.predict(probe)
            fell_back = any(
                "Device is changed from GPU to CPU" in str(w.message) or "No visible GPU is found" in str(w.message)
                for w in caught
            )
            if not fell_back:
                logger.info("Static model bound to GPU (CUDA).")
                return model
        except Exception:
            pass
    model.set_params(device="cpu", n_jobs=-1)
    logger.info("Static model bound to CPU (all cores).")
    return model


def _estimate_aoi_pixels(aoi, transform, grid_pixels: int) -> int:
    """How many raster pixels the AOI polygon itself covers.

    Computed geometrically (projected AOI area / projected pixel area) rather than by
    rasterising, which would cost a full extra pass over the grid.
    """
    try:
        centroid = aoi.geometry.union_all().centroid if hasattr(aoi.geometry, "union_all")             else aoi.geometry.unary_union.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        utm_epsg = (32600 if centroid.y >= 0 else 32700) + utm_zone
        projected = aoi.to_crs(epsg=utm_epsg)

        pixel_width_deg, pixel_height_deg = abs(transform.a), abs(transform.e)
        bounds = projected.total_bounds
        degrees_bounds = aoi.total_bounds
        metres_per_degree_x = (bounds[2] - bounds[0]) / max(degrees_bounds[2] - degrees_bounds[0], 1e-12)
        metres_per_degree_y = (bounds[3] - bounds[1]) / max(degrees_bounds[3] - degrees_bounds[1], 1e-12)

        pixel_area_m2 = (pixel_width_deg * metres_per_degree_x) * (pixel_height_deg * metres_per_degree_y)
        aoi_area_m2 = float(projected.geometry.area.sum())
        if pixel_area_m2 > 0:
            return max(1, int(aoi_area_m2 / pixel_area_m2))
    except Exception as exc:
        logger.warning(f"Could not measure the AOI's pixel count ({exc}); falling back to the full grid.")
    return grid_pixels


def assert_grid_parity(path_a: str, path_b: str) -> None:
    """Hard gate: two rasters must share CRS, transform and dimensions."""
    with rasterio.open(path_a) as a, rasterio.open(path_b) as b:
        assert a.crs == b.crs, f"CRS mismatch: {a.crs} vs {b.crs}"
        assert (a.width, a.height) == (b.width, b.height), (
            f"Dimension mismatch: {a.width}x{a.height} vs {b.width}x{b.height}"
        )
        assert np.allclose(tuple(a.transform), tuple(b.transform), atol=1e-9), "Transform mismatch"


def build_crop_mask(
    static_image_path: str,
    ndvi_classification_path: str,
    aoi_path: str,
    output_mask_path: str,
    crop_classes: Sequence[int] = (1,),
    chunk_size: int = 2048,
    max_coverage_fraction: float = 0.90,
) -> None:
    """Warps the NDVI/RF sieved classification onto the static image's grid, keeping
    only `crop_classes` pixels that also fall inside the AOI. This is the mask that
    confines the static model to ground the NDVI pipeline already called crop."""
    logger.info(f"Building crop mask | crop_classes={tuple(crop_classes)} | AOI={aoi_path}")

    aoi = gpd.read_file(aoi_path)
    if aoi.empty:
        raise ValueError(f"AOI contains no features: {aoi_path}")
    invalid = ~aoi.geometry.is_valid
    if invalid.any():
        logger.warning(f"Repairing {int(invalid.sum())} invalid AOI geometr(ies) before masking.")
        aoi.loc[invalid, "geometry"] = aoi.loc[invalid, "geometry"].make_valid()

    with rasterio.open(static_image_path) as target:
        mask_profile = target.profile.copy()
        target_crs, target_transform = target.crs, target.transform
        target_width, target_height = target.width, target.height

    if aoi.crs != target_crs:
        aoi = aoi.to_crs(target_crs)
    geometries = aoi.geometry.values
    spatial_index = aoi.sindex

    mask_profile.update(driver="GTiff", count=1, dtype=rasterio.uint8, nodata=0,
                        compress="lzw", tiled=True, blockxsize=256, blockysize=256, bigtiff="YES")

    crop_class_array = np.asarray(crop_classes)
    kept_pixels = 0

    # Measure against the AOI, not the raster grid. The grid is the AOI's bounding box,
    # and an irregular AOI fills only part of it (42% for one test AOI), so a bbox
    # denominator can neither fire on a genuinely degenerate mask nor report an honest
    # crop share -- while a rectangular AOI that really is one crop would trip it.
    aoi_pixels = _estimate_aoi_pixels(aoi, target_transform, target_width * target_height)

    with rasterio.open(ndvi_classification_path) as source:
        warp_options = {
            "resampling": Resampling.nearest, "crs": target_crs, "transform": target_transform,
            "height": target_height, "width": target_width,
            "nodata": source.nodata if source.nodata is not None else 0,
        }
        with WarpedVRT(source, **warp_options) as warped, rasterio.open(output_mask_path, "w", **mask_profile) as dst:
            row_offsets = range(0, target_height, chunk_size)
            col_offsets = range(0, target_width, chunk_size)
            with tqdm(total=len(row_offsets) * len(col_offsets), desc="Building crop mask", unit="block") as progress:
                for row_off in row_offsets:
                    for col_off in col_offsets:
                        window_height = min(chunk_size, target_height - row_off)
                        window_width = min(chunk_size, target_width - col_off)
                        window = Window(col_off, row_off, window_width, window_height)
                        empty_block = np.zeros((window_height, window_width), dtype=np.uint8)

                        # Skip windows the AOI does not reach at all -- no read, no rasterise.
                        window_bounds = rasterio.windows.bounds(window, target_transform)
                        candidate_ids = list(spatial_index.query(box(*window_bounds)))
                        if not candidate_ids:
                            dst.write(empty_block, 1, window=window)
                            progress.update(1)
                            continue

                        class_block = np.isin(warped.read(1, window=window), crop_class_array)
                        if not class_block.any():
                            dst.write(empty_block, 1, window=window)
                            progress.update(1)
                            continue

                        inside_aoi = geometry_mask(
                            geometries[candidate_ids],
                            out_shape=(window_height, window_width),
                            transform=rasterio.windows.transform(window, target_transform),
                            invert=True,
                            all_touched=False,
                        )
                        block = (class_block & inside_aoi).astype(np.uint8)
                        kept_pixels += int(block.sum())
                        dst.write(block, 1, window=window)
                        progress.update(1)

    coverage = kept_pixels / aoi_pixels if aoi_pixels else 0.0
    logger.info(f"Crop mask coverage: {kept_pixels:,} px ({coverage:.2%} of the AOI)")
    if coverage == 0:
        logger.warning(f"Crop mask is empty: classes {tuple(crop_classes)} not present in this AOI.")
    assert coverage < max_coverage_fraction, (
        f"Degenerate crop mask: {coverage:.2%} of the AOI exceeds {max_coverage_fraction:.0%}; "
        "the mask is likely admitting background classes."
    )
    assert_grid_parity(static_image_path, output_mask_path)
    logger.info("Grid parity verified: crop mask is 1:1 with the static image.")


def classify_window(
    static_image_path: str,
    crop_mask_path: Optional[str],
    window: Window,
    model_path: str,
    use_mask: bool,
    model_positive_class: int,
    crop_label: int,
    background_label: int,
) -> Tuple[Window, np.ndarray]:
    """Classifies one window of the static image. Runs inside a pool worker."""
    model = get_static_model(model_path)
    src = _open_cached(static_image_path)

    nodata_value = src.nodata if src.nodata is not None else 0
    band_count = src.count
    window_height, window_width = int(window.height), int(window.width)
    labels = np.full(window_height * window_width, background_label, dtype=np.uint8)

    if use_mask:
        assert crop_mask_path is not None, "use_mask=True but no crop mask was provided."
        mask_src = _open_cached(crop_mask_path)
        assert (mask_src.width, mask_src.height) == (src.width, src.height), "Crop mask / image grid mismatch."
        in_crop = mask_src.read(1, window=window) == 1
    else:
        in_crop = np.ones((window_height, window_width), dtype=bool)

    if not in_crop.any():
        return window, labels.reshape((window_height, window_width))

    block = src.read(window=window)
    if isinstance(nodata_value, float) and np.isnan(nodata_value):
        has_data = np.any(~np.isnan(block), axis=0)
    else:
        has_data = np.any(block != nodata_value, axis=0)

    classify_here = (has_data & in_crop).ravel()
    if classify_here.any():
        features = block.reshape(band_count, -1).T[classify_here].astype(np.float32)
        raw_predictions = model.predict(features)
        labels[classify_here] = np.where(
            raw_predictions == model_positive_class, crop_label, background_label
        ).astype(np.uint8)

    return window, labels.reshape((window_height, window_width))


def classify_static_image(
    static_image_path: str,
    output_path: str,
    ndvi_classification_path: Optional[str],
    aoi_path: Optional[str],
    model_path: str,
    crop_classes: Sequence[int] = (1,),
    chunk_size: int = 2048,
    use_mask: bool = True,
    model_positive_class: int = 1,
    crop_label: int = 1,
    background_label: int = 0,
    worker_count: Optional[int] = None,
    output_nodata: Optional[int] = None,
    memory_fraction: float = 0.5,
    model_memory_expansion: float = 12.0,
    memory_budget_bytes: Optional[int] = None,
) -> Optional[str]:
    """Windowed, multiprocess XGBoost inference over the static image.

    The model is referenced by path rather than passed as an object, so nothing large is
    pickled per task; each worker loads and caches its own CPU-only copy. Returns the
    path of the temporary crop mask (or None when `use_mask` is False).
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    crop_mask_path = None
    if use_mask:
        assert ndvi_classification_path and aoi_path, (
            "use_mask=True requires both ndvi_classification_path and aoi_path; "
            "refusing to silently classify every pixel."
        )
        crop_mask_path = str(Path(output_path).parent / f"{Path(output_path).stem}_crop_mask.tif")
        build_crop_mask(
            static_image_path=static_image_path,
            ndvi_classification_path=ndvi_classification_path,
            aoi_path=aoi_path,
            output_mask_path=crop_mask_path,
            crop_classes=crop_classes,
            chunk_size=chunk_size,
        )

    windows: List[Window] = []
    with rasterio.open(static_image_path) as src:
        output_profile = src.profile
        image_width, image_height = src.width, src.height
        source_band_count = src.count
        source_dtype_size = np.dtype(src.dtypes[0]).itemsize
        for row_off in range(0, image_height, chunk_size):
            for col_off in range(0, image_width, chunk_size):
                windows.append(Window(col_off, row_off,
                                      min(chunk_size, image_width - col_off),
                                      min(chunk_size, image_height - row_off)))

    # Force GTiff explicitly: `static_image_path` may be a `.vrt` (STAC's default static
    # mosaic), whose profile driver is "VRT". Inheriting that would produce an output
    # that fails on any later write with "Writing through VRTSourcedRasterBand is not
    # supported."
    #
    # `nodata` defaults to unset. `background_label` is a real class ("non-crop"), not
    # missing data -- tagging it as nodata (which the original notebooks did) makes QGIS
    # and every masked read treat all non-crop pixels as absent, so the raster looks like
    # it contains only the crop label. Pass `output_nodata` to tag one anyway.
    output_profile.update(driver="GTiff", dtype=rasterio.uint8, count=1, nodata=output_nodata,
                          compress="lzw", tiled=True, blockxsize=256, blockysize=256, BIGTIFF="YES")

    worker_count = resolve_worker_count(
        requested=worker_count, window_count=len(windows), model_path=model_path,
        memory_fraction=memory_fraction, model_memory_expansion=model_memory_expansion,
        per_worker_window_bytes=window_working_bytes(
            chunk_size, source_band_count, source_dtype_size),
        memory_budget_bytes=memory_budget_bytes,
    )
    logger.info(f"Classifying {len(windows)} window(s) across {worker_count} worker(s)...")
    spawn_context = multiprocessing.get_context("spawn")

    # Classify into a temporary file and rename only once every window has been written,
    # the same discipline the NDVI tile workers use. Writing straight to `output_path`
    # means an interrupted run (a kill, an OOM, a dropped session) leaves a truncated or
    # zero-byte raster at the final path, which a later resume then treats as finished
    # work and reads -- failing far from the cause.
    temporary_path = f"{output_path}.tmp.tif"
    try:
        with rasterio.open(temporary_path, "w", **output_profile) as dst:
            with ProcessPoolExecutor(max_workers=worker_count, mp_context=spawn_context) as pool:
                futures = {
                    pool.submit(classify_window, static_image_path, crop_mask_path, window, model_path,
                                use_mask, model_positive_class, crop_label, background_label): window
                    for window in windows
                }
                with tqdm(total=len(windows), desc="Classifying static image", unit="block") as progress:
                    for future in as_completed(futures):
                        try:
                            window, labels = future.result()
                            dst.write(labels, 1, window=window)
                        except Exception:
                            logger.error(f"Window failed at {futures[future]}", exc_info=True)
                            raise
                        finally:
                            progress.update(1)
    except BaseException:
        # BaseException, not Exception: KeyboardInterrupt is exactly the case that leaves
        # the half-written file behind.
        Path(temporary_path).unlink(missing_ok=True)
        raise

    os.replace(temporary_path, output_path)
    return crop_mask_path
