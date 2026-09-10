"""Whittaker-smoothing + Random Forest inference on one local NDVI-stack tile.

Source-agnostic: works on any local GeoTIFF whose band descriptions
`band_utils.parse_band_stack` understands, whether it was downloaded from GCS (GEE
acquisition) or written directly by farmdar.sentinel (STAC acquisition). Lives in a
real module (not a notebook `%%writefile` cell) because ProcessPoolExecutor needs to
pickle a top-level, importable function.

Two things here differ deliberately from the original notebooks:

* The model is passed **by path**, not as an object. The notebooks pickled the whole
  RandomForest into every submitted task -- hundreds of MB copied per tile, per worker.
  Each worker process now loads it once and caches it in `_MODEL_CACHE`.
* Partially-valid pixels are smoothed by **grouping pixels that share a gap pattern**
  and solving each group as one batched linear system, instead of
  `np.apply_along_axis` running a Python-level solve per pixel. Same arithmetic, same
  results, far less time.
"""
from __future__ import annotations

import gc
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window

import raster_io
from band_utils import parse_band_stack

# Per-process model cache: a worker loads the RandomForest once and reuses it for
# every tile it handles.
_MODEL_CACHE: Dict[str, Any] = {}

# Caps the temporary float64 buffer a single batched solve can allocate.
_SOLVE_BATCH_ROWS = 50_000


def get_rf_model(model_path: str) -> Any:
    """Loads (and caches per process) the RandomForest used for NDVI inference."""
    model = _MODEL_CACHE.get(model_path)
    if model is None:
        import joblib

        model = joblib.load(model_path)
        # Single-threaded inside each worker: the pool already provides parallelism, and
        # nested thread pools deadlock under fork/spawn.
        try:
            model.n_jobs = 1
        except AttributeError:
            pass
        _MODEL_CACHE[model_path] = model
    return model


def get_penalty_matrix(n_timesteps: int, smoothing_lambda: float, difference_order: int) -> np.ndarray:
    identity = np.eye(n_timesteps)
    differences = np.diff(identity, n=difference_order, axis=0)
    return smoothing_lambda * (differences.T @ differences)


def _solve_batched(system_matrix: np.ndarray, right_hand_sides: np.ndarray) -> np.ndarray:
    """Solves `system_matrix @ x = rhs` for many right-hand sides, in row batches so the
    float64 working buffer stays bounded regardless of how many pixels are involved."""
    n_rows = right_hand_sides.shape[0]
    solutions = np.empty_like(right_hand_sides)
    for start in range(0, n_rows, _SOLVE_BATCH_ROWS):
        stop = min(start + _SOLVE_BATCH_ROWS, n_rows)
        block = right_hand_sides[start:stop]
        try:
            solutions[start:stop] = np.linalg.solve(system_matrix, block.T).T
        except np.linalg.LinAlgError:
            solutions[start:stop] = np.linalg.lstsq(system_matrix, block.T, rcond=None)[0].T
    return solutions


def smooth_ndvi_block(
    ndvi_block: np.ndarray,
    penalty_matrix: np.ndarray,
    clip_bounds: Optional[Tuple[float, float]],
    missing_value: Optional[float],
) -> np.ndarray:
    """Whittaker-smooths a (bands, height, width) NDVI block along the time axis.

    Pixels with no gaps share one system matrix and are solved in one batch. Pixels with
    gaps are grouped by their exact gap pattern -- every pixel in a group shares the same
    system matrix, so each group is also one batched solve.
    """
    n_bands, height, width = ndvi_block.shape
    pixels = ndvi_block.transpose(1, 2, 0).reshape(-1, n_bands).astype(np.float32)

    if missing_value is None or np.isnan(missing_value):
        missing = np.isnan(pixels)
    else:
        missing = pixels == missing_value

    missing_per_pixel = missing.sum(axis=1)
    complete_rows = np.flatnonzero(missing_per_pixel == 0)
    partial_rows = np.flatnonzero((missing_per_pixel > 0) & (missing_per_pixel < n_bands))

    if complete_rows.size:
        full_system = np.eye(n_bands) + penalty_matrix
        pixels[complete_rows] = _solve_batched(full_system, pixels[complete_rows])

    if partial_rows.size:
        # Group pixels by identical gap pattern: one packed byte-string per pattern.
        # Cloud gaps are spatially coherent, so neighbouring pixels in a block usually
        # share a pattern and each group becomes one batched solve instead of many.
        packed = np.packbits(missing[partial_rows], axis=1)
        packed_view = np.ascontiguousarray(packed).view(
            np.dtype((np.void, packed.dtype.itemsize * packed.shape[1]))
        ).ravel()
        _, group_ids = np.unique(packed_view, return_inverse=True)

        # Sort once and walk contiguous runs. Testing `group_ids == g` per group instead
        # would rescan every partial pixel for every group -- quadratic when gap patterns
        # barely repeat.
        order = np.argsort(group_ids, kind="stable")
        group_boundaries = np.flatnonzero(np.diff(group_ids[order])) + 1

        for member_positions in np.split(order, group_boundaries):
            members = partial_rows[member_positions]
            gap_pattern = missing[members[0]]

            # Identical to the per-pixel formulation: W = diag(observed), A = W + P, and
            # the right-hand side is zero wherever the series has no observation.
            system = np.diag((~gap_pattern).astype(float)) + penalty_matrix
            right_hand_sides = pixels[members]
            right_hand_sides[:, gap_pattern] = 0.0
            pixels[members] = _solve_batched(system, right_hand_sides)

    if clip_bounds is not None:
        touched = np.concatenate([complete_rows, partial_rows]) if partial_rows.size else complete_rows
        if touched.size:
            # Assign back explicitly: `pixels[touched]` is a fancy-index copy, so an
            # `out=` clip would land in a temporary and be thrown away.
            pixels[touched] = np.clip(pixels[touched], clip_bounds[0], clip_bounds[1])

    del missing, missing_per_pixel
    return pixels.reshape(height, width, n_bands).transpose(2, 0, 1)


def worker_process_tile(
    tile_path: Union[str, Path],
    output_dir: Union[str, Path],
    model_path: str,
    inference_start_date: str,
    inference_end_date: str,
    smoothing_lambda: float = 0.5,
    difference_order: int = 2,
    clip_bounds: Tuple[float, float] = (-1.0, 1.0),
    nodata_label: int = 255,
    export_smoothed_stack: bool = False,
) -> Dict[str, Any]:
    """Reads one local NDVI-stack tile, Whittaker-smooths its full time series, subsets
    to the inference date window, and runs RF `.predict()` per pixel. Processes one
    raster block at a time, so memory is independent of tile size."""
    with rasterio.Env(GDAL_NUM_THREADS="1", OMP_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1"):
        tile_path = Path(tile_path)
        output_dir = Path(output_dir)
        prediction_path = output_dir / tile_path.name.replace(".tif", "_predicted.tif")
        prediction_tmp_path = prediction_path.with_suffix(".tmp.tif")
        smoothed_path = output_dir / tile_path.name.replace(".tif", "_smoothed.tif") if export_smoothed_stack else None
        smoothed_tmp_path = smoothed_path.with_suffix(".tmp.tif") if export_smoothed_stack else None

        # Resume check: read a 1x1 block to confirm the LZW stream isn't truncated.
        if prediction_path.exists():
            try:
                with rasterio.open(prediction_path) as existing:
                    existing.read(1, window=Window(0, 0, 1, 1))
                return {"tile": tile_path, "prediction": prediction_path, "smoothed": smoothed_path}
            except Exception:
                prediction_path.unlink(missing_ok=True)

        rf_model = get_rf_model(model_path)

        try:
            with rasterio.open(tile_path) as src:
                red_indices, nir_indices, band_dates = parse_band_stack(src.descriptions)
                if not band_dates:
                    raise ValueError(f"No dated red/NIR band pairs found in {tile_path.name}")

                date_index = pd.to_datetime(band_dates, errors="coerce")
                in_window = (
                    (date_index >= pd.to_datetime(inference_start_date))
                    & (date_index <= pd.to_datetime(inference_end_date))
                )
                inference_band_positions = np.flatnonzero(in_window)
                if inference_band_positions.size == 0:
                    raise ValueError(
                        f"{tile_path.name}: no bands fall inside the inference window "
                        f"{inference_start_date}..{inference_end_date}"
                    )

                penalty_matrix = get_penalty_matrix(len(band_dates), smoothing_lambda, difference_order)

                output_profile = {
                    "driver": "GTiff", "height": src.height, "width": src.width,
                    "transform": src.transform, "crs": src.crs, "dtype": rasterio.uint8,
                    "count": 1, "nodata": nodata_label, "compress": "lzw", "tiled": True,
                    "blockxsize": 256, "blockysize": 256, "predictor": 2, "bigtiff": "YES",
                }

                smoothed_dst = None
                if export_smoothed_stack:
                    smoothed_profile = dict(output_profile, dtype="float32", count=len(band_dates), predictor=3)
                    smoothed_dst = rasterio.open(smoothed_tmp_path, "w", **smoothed_profile)
                    for position, date_text in enumerate(band_dates, start=1):
                        smoothed_dst.set_band_description(position, f"NDVI_{date_text.replace('-', '_')}")

                try:
                    with rasterio.open(prediction_tmp_path, "w", **output_profile) as dst:
                        for _, window in src.block_windows(1):
                            raw_block = src.read(window=window)
                            red = raw_block[red_indices].astype(np.float32)
                            nir = raw_block[nir_indices].astype(np.float32)
                            del raw_block

                            denominator = nir + red
                            ndvi_block = np.full_like(red, np.nan)
                            np.divide(nir - red, denominator, out=ndvi_block, where=denominator > 0)
                            del red, nir, denominator

                            smoothed_block = smooth_ndvi_block(ndvi_block, penalty_matrix, clip_bounds, np.nan)
                            del ndvi_block

                            if smoothed_dst is not None:
                                smoothed_dst.write(smoothed_block, window=window)

                            window_series = smoothed_block[inference_band_positions]
                            del smoothed_block
                            n_dates, height, width = window_series.shape
                            feature_rows = window_series.transpose(1, 2, 0).reshape(-1, n_dates)

                            has_signal = ~np.all(np.isnan(feature_rows) | (feature_rows == 0), axis=1)
                            predictions = np.full(feature_rows.shape[0], nodata_label, dtype=np.uint8)
                            if has_signal.any():
                                usable = np.nan_to_num(feature_rows[has_signal], nan=0.0)
                                predictions[has_signal] = rf_model.predict(usable).astype(np.uint8)
                                del usable

                            dst.write(predictions.reshape(height, width), 1, window=window)
                            del window_series, feature_rows, has_signal, predictions

                    prediction_tmp_path.replace(prediction_path)
                finally:
                    if smoothed_dst is not None:
                        smoothed_dst.close()
                        if smoothed_tmp_path and smoothed_tmp_path.exists():
                            smoothed_tmp_path.replace(smoothed_path)
        finally:
            prediction_tmp_path.unlink(missing_ok=True)
            if smoothed_tmp_path is not None:
                smoothed_tmp_path.unlink(missing_ok=True)
            gc.collect()

        return {"tile": tile_path, "prediction": prediction_path, "smoothed": smoothed_path}


def mosaic_prediction_tiles(
    prediction_paths: Sequence[Union[str, Path]], output_path: Union[str, Path],
    band_name: str = "RF_Classification", nodata_label: int = 255,
) -> Path:
    """Streams per-tile classification rasters into one district-wide GeoTIFF."""
    return raster_io.mosaic_geotiffs(prediction_paths, output_path, nodata=nodata_label, band_name=band_name)


def generate_global_index_mask(reference_path: Path, output_path: Path, nodata_value: int = -1) -> None:
    """Writes an int32 raster whose valid pixels carry their global flat index."""
    with rasterio.open(reference_path) as src:
        profile = src.profile.copy()
        raster_width = src.width
        reference_nodata = src.nodata if src.nodata is not None else 255
        profile.update(driver="GTiff", dtype=rasterio.int32, count=1, nodata=nodata_value,
                       compress="lzw", tiled=True, blockxsize=256, blockysize=256, bigtiff="YES")

        with rasterio.open(output_path, "w", **profile) as dst:
            for _, window in src.block_windows(1):
                block = src.read(1, window=window)
                valid = block != reference_nodata
                rows, cols = np.indices((window.height, window.width))
                flat_indices = ((rows + window.row_off) * raster_width + (cols + window.col_off)).astype(np.int32)
                index_block = np.full(block.shape, nodata_value, dtype=np.int32)
                index_block[valid] = flat_indices[valid]
                dst.write(index_block, 1, window=window)
