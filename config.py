"""Unified pipeline configuration: crop presets, source switches, model sources.

The two independent source switches (`ndvi_source`, `static_source`) plus their
sub-modes are the point of this module; everything else is the per-crop configuration
the three original notebooks each hardcoded separately.

Model files are described by `ModelSource`, which supports three ways of pointing at a
model -- a local path, a `gs://` URI (downloaded once and cached permanently), or an
explicit dict with a per-model service-account key -- so different models can live in
different buckets under different credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

NdviSource = Literal["gee", "stac"]
StaticSource = Literal["stac", "gee"]
StacStaticMode = Literal["auto", "manual"]
GeeStaticMode = Literal["api_auto", "api_manual", "manual_gcs_link"]

# Canonical static-image band order shared by both acquisition backends. The XGBoost
# static classifier reads pixels positionally, so what matters is that both backends
# fetch bands in this exact order -- band *names* never need to match between GEE and
# STAC as long as the order does.
STATIC_BAND_ORDER_STAC = ["blue", "green", "red", "rededge1", "nir", "ndvi"]
STATIC_BAND_ORDER_GEE = ["B2", "B3", "B4", "B5", "B8", "NDVI"]

# Permanent on-disk cache for models pulled from GCS. Survives across runs and across
# pipeline versions, so a model is downloaded at most once per machine.
DEFAULT_MODEL_CACHE_DIR = os.path.expanduser("~/.cache/fao_pipeline/models")
DEFAULT_AOI_CACHE_DIR = os.path.expanduser("~/.cache/fao_pipeline/aoi")


@dataclass
class ModelSource:
    """Where a trained model file lives.

    Exactly one of `local_path` / `gcs_uri` is required:

    * `local_path` -- use this file as-is, no download, no cache (manual override).
    * `gcs_uri`    -- ``gs://bucket/path/model.json``; downloaded once into the model
      cache and reused from there on every later run.
    * `gcs_key_path` -- service-account JSON for *this* model's bucket. Optional; when
      omitted the pipeline's own GEE/GCS key is used. Set it when a model lives in a
      bucket that a different service account owns.

    Accepts shorthand: a plain string is treated as a `gcs_uri` if it starts with
    ``gs://`` and as a `local_path` otherwise (see `coerce`).
    """

    local_path: Optional[str] = None
    gcs_uri: Optional[str] = None
    gcs_key_path: Optional[str] = None

    @classmethod
    def coerce(cls, value: Union["ModelSource", str, Path, Dict[str, Any]]) -> "ModelSource":
        if isinstance(value, ModelSource):
            return value
        if isinstance(value, dict):
            return cls(**value)
        if isinstance(value, (str, Path)):
            text = str(value)
            return cls(gcs_uri=text) if text.startswith("gs://") else cls(local_path=text)
        raise TypeError(f"Cannot interpret {value!r} as a ModelSource.")

    def validate(self, label: str) -> None:
        if bool(self.local_path) == bool(self.gcs_uri):
            raise ValueError(
                f"{label}: set exactly one of local_path / gcs_uri (got "
                f"local_path={self.local_path!r}, gcs_uri={self.gcs_uri!r})."
            )
        if self.gcs_key_path and not self.gcs_uri:
            raise ValueError(f"{label}: gcs_key_path is only meaningful together with gcs_uri.")

    def describe(self) -> str:
        if self.local_path:
            return f"local:{self.local_path}"
        key = f" (key: {self.gcs_key_path})" if self.gcs_key_path else ""
        return f"{self.gcs_uri}{key}"


def get_crop_config(crop_name: str, year_str: str) -> dict:
    """Per-crop defaults: class labels, sieve/area thresholds, date windows, models.

    Class-label vocabulary (three distinct label spaces, easy to confuse):
      * `ndvi_crop_classes`  -- class values in the NDVI/RF classification map that
        represent the crop. Used to sieve that map, to build the static model's mask,
        and as the vectorization target when the static model is disabled.
      * `static_model_positive_class` / `static_crop_label` / `static_background_label`
        -- the raw class the XGBoost model emits for "crop", the label written for
        those pixels in the static output, and the label for everything else.
      * `output_polygon_label` -- the label stamped on the exported polygons.
    """
    current_year = int(year_str)
    prev_year = current_year - 1

    configs = {
        "cane": {
            "ndvi_crop_classes": [1],
            "sieve_min_pixel_size": 20,
            "min_polygon_area_acres": 0.5,
            "static_model_positive_class": 1,
            "static_crop_label": 1,
            "static_background_label": 4,
            "output_polygon_label": 1,
            "ndvi_series_start": f"{prev_year}-12-24",
            "ndvi_series_end": f"{current_year}-11-17",
            "ndvi_inference_start": f"{current_year}-01-01",
            "ndvi_inference_end": f"{current_year}-11-15",
            "static_window_start": f"{current_year}-10-15",
            "static_window_end": f"{current_year}-11-15",
            "ndvi_model": "gs://farmdar_data_catalog/fao_cane_model_file/fao_cane_rf_model.joblib",
            "static_model": "gs://farmdar_data_catalog/fao_cane_model_file/fao_cane_xgb_model.json",
        },
        "spr_maize": {
            "ndvi_crop_classes": [1, 4, 5, 6, 7],
            "sieve_min_pixel_size": 20,
            "min_polygon_area_acres": 0.5,
            "static_model_positive_class": 1,
            "static_crop_label": 1,
            "static_background_label": 8,
            "output_polygon_label": 3015,
            "ndvi_series_start": f"{prev_year}-12-24",
            "ndvi_series_end": f"{current_year}-07-17",
            "ndvi_inference_start": f"{current_year}-01-01",
            "ndvi_inference_end": f"{current_year}-07-15",
            "static_window_start": f"{current_year}-05-01",
            "static_window_end": f"{current_year}-05-15",
            "ndvi_model": "/home/jovyan/FAO/spr_maize/model_files/spr_maize_rf_classifier_15072026.joblib",
            "static_model": "/home/jovyan/FAO/spr_maize/model_files/static_image/xgb_model_20250505_075148.json",
        },
        "wheat": {
            "ndvi_crop_classes": [14],
            "sieve_min_pixel_size": 20,
            "min_polygon_area_acres": 0.5,
            "static_model_positive_class": 1,
            "static_crop_label": 1,
            "static_background_label": 4,
            "output_polygon_label": 14,
            "ndvi_series_start": f"{prev_year}-08-24",
            "ndvi_series_end": f"{current_year}-07-01",
            "ndvi_inference_start": f"{prev_year}-09-01",
            "ndvi_inference_end": f"{current_year}-06-30",
            "static_window_start": f"{current_year}-02-01",
            "static_window_end": f"{current_year}-02-25",
            "ndvi_model": "/home/jovyan/FAO/wheat/model_files/fao_wheat_timeseries_model_v1.joblib",
            "static_model": "/home/jovyan/FAO/wheat/model_files/static_models/xgboost_wheat_model_v1.json",
        },
    }

    if crop_name not in configs:
        raise ValueError(f"No crop config defined for '{crop_name}'. Known crops: {list(configs)}")
    return configs[crop_name]


@dataclass
class PipelineConfig:
    # ------------------------------------------------------------------ identity
    crop: str
    year: str
    district_name: str
    aoi_path: str          # resolved local path (see aoi_io.resolve_aoi)
    base_dir: str = "/home/jovyan/FAO"
    output_basename: str = ""
    aoi_source: str = ""   # what the user originally passed, kept for reporting
    aoi_gcs_key_path: Optional[str] = None  # credentials for a gs:// AOI, if different
    aoi_cache_dir: str = DEFAULT_AOI_CACHE_DIR
    output_dir: Optional[str] = None  # overrides pipeline.default_output_dir

    # ------------------------------------- the two independent source switches
    ndvi_source: NdviSource = "stac"
    static_source: StaticSource = "stac"
    run_static_model: bool = True

    # ------------------------------------------------------------- STAC options
    # (passed straight through to farmdar.sentinel, which is never modified)
    stac_ndvi_max_cloud_pct: int = 97
    stac_static_mode: StacStaticMode = "auto"
    stac_static_selection: dict = field(default_factory=lambda: dict(
        cloud_lt=80, n_dates=2, selection_mode="greedy", cloud_metric="aoi"))
    stac_static_dates: Optional[List[str]] = None  # required when stac_static_mode == "manual"
    stac_resolution_m: int = 10
    stac_tile_size_deg: float = 0.1
    stac_worker_count: int = 8

    # -------------------------------------------------------------- GEE options
    gee_project_name: str = "farmdar"
    gee_service_account_key: Optional[str] = None  # default: gcs_data_downloader_ee_{gee_project_name}.json
    gcs_bucket: str = "farmdar_data_catalog"
    gcs_base_folder: Optional[str] = None  # default: fao_{crop}_{year}
    gee_grid_cell_acres: int = 15000
    gee_landsat_cutover_year: int = 2018  # year < this -> Landsat 8 only (never Landsat 7)
    gee_sentinel_resolution_m: int = 10
    gee_landsat_resolution_m: int = 30
    gee_static_mode: GeeStaticMode = "api_auto"
    gee_static_single_date: Optional[str] = None  # api_manual, single-date composite
    gee_static_top_date: Optional[str] = None     # api_manual, two-date mosaic (top layer)
    gee_static_bottom_date: Optional[str] = None  # api_manual, two-date mosaic (bottom layer)
    gee_static_gcs_uri: Optional[str] = None      # required when gee_static_mode == "manual_gcs_link"
    gee_wait_for_exports: bool = True
    gee_export_submit_workers: int = 4

    # ------------------------------------------------------------ model sources
    ndvi_model: Any = None    # ModelSource | "gs://..." | "/local/path" | {...}
    static_model: Any = None
    model_cache_dir: str = DEFAULT_MODEL_CACHE_DIR

    # ------------------------------------------------- compute / memory limits
    composite_step_days: int = 8            # NDVI compositing window (both GEE and STAC)
    ndvi_worker_count: Optional[int] = None  # default: 75% of CPU cores
    # Recycle each NDVI worker process after this many tiles: bounds the GDAL/numpy
    # memory a long-lived worker can accumulate, while still amortising the (slow)
    # RandomForest load across several tiles. Set 1 for maximum memory safety.
    ndvi_worker_max_tasks: int = 8
    static_worker_count: Optional[int] = None  # default: CPU cores - 1
    static_chunk_size: int = 2048
    ndvi_tile_timeout_s: int = 900

    # ------------------------------------------------ classification thresholds
    ndvi_crop_classes: List[int] = field(default_factory=list)
    sieve_min_pixel_size: int = 20
    min_polygon_area_acres: float = 0.5
    static_model_positive_class: int = 1
    static_crop_label: int = 1
    static_background_label: int = 4
    output_polygon_label: int = 1
    ndvi_nodata_label: int = 255
    # The static output's background label is a real class, so by default no nodata tag
    # is written (see static_classify.classify_static_image). Set a value here only if a
    # downstream tool needs one -- it will render those pixels as absent.
    static_output_nodata: Optional[int] = None

    # ------------------------------------------------------------ date windows
    ndvi_series_start: str = ""
    ndvi_series_end: str = ""
    ndvi_inference_start: str = ""
    ndvi_inference_end: str = ""
    static_window_start: str = ""
    static_window_end: str = ""

    # ----------------------------------------------------------------- outputs
    export_shapefile_zip: bool = True
    dissolve_polygons: bool = False  # see postprocess.vectorize_process_and_export

    # ------------------------------------------------------- local-disk cleanup
    delete_raw_ndvi_tiles: bool = True
    delete_raw_static_tiles: bool = True

    # ------------------------------------------------------------------ helpers
    @property
    def gee_resolution_m(self) -> int:
        return self.gee_sentinel_resolution_m if self.uses_sentinel else self.gee_landsat_resolution_m

    @property
    def uses_sentinel(self) -> bool:
        return int(self.year) >= self.gee_landsat_cutover_year

    @property
    def gee_sensor_mode(self) -> str:
        return "SENTINEL" if self.uses_sentinel else "LANDSAT"

    @property
    def needs_gee_api(self) -> bool:
        """True when this run actually calls the Earth Engine API. A static-only run in
        `manual_gcs_link` mode touches GCS but never the EE API."""
        if self.ndvi_source == "gee":
            return True
        return (
            self.run_static_model
            and self.static_source == "gee"
            and self.gee_static_mode in ("api_auto", "api_manual")
        )

    @property
    def needs_gcs(self) -> bool:
        if self.needs_gee_api:
            return True
        return self.run_static_model and self.static_source == "gee"

    def validate(self) -> None:
        """Fails fast on inconsistent settings, before any expensive acquisition."""
        if self.ndvi_source not in ("gee", "stac"):
            raise ValueError(f"ndvi_source must be 'gee' or 'stac', got {self.ndvi_source!r}.")
        if self.static_source not in ("gee", "stac"):
            raise ValueError(f"static_source must be 'gee' or 'stac', got {self.static_source!r}.")

        if not Path(self.aoi_path).exists():
            raise FileNotFoundError(f"AOI not found: {self.aoi_path}")

        ModelSource.coerce(self.ndvi_model).validate("ndvi_model")
        if self.run_static_model:
            ModelSource.coerce(self.static_model).validate("static_model")

        if self.run_static_model and self.static_source == "stac":
            if self.stac_static_mode not in ("auto", "manual"):
                raise ValueError(f"stac_static_mode must be 'auto' or 'manual', got {self.stac_static_mode!r}.")
            if self.stac_static_mode == "manual" and not self.stac_static_dates:
                raise ValueError("stac_static_mode='manual' requires stac_static_dates=['YYYY-MM-DD', ...].")

        if self.run_static_model and self.static_source == "gee":
            if self.gee_static_mode == "manual_gcs_link" and not self.gee_static_gcs_uri:
                raise ValueError("gee_static_mode='manual_gcs_link' requires gee_static_gcs_uri='gs://...'.")
            if self.gee_static_mode == "api_manual":
                has_single = bool(self.gee_static_single_date)
                has_pair = bool(self.gee_static_top_date and self.gee_static_bottom_date)
                if not (has_single or has_pair):
                    raise ValueError(
                        "gee_static_mode='api_manual' requires gee_static_single_date, "
                        "or both gee_static_top_date and gee_static_bottom_date."
                    )
            if self.gee_static_mode not in ("api_auto", "api_manual", "manual_gcs_link"):
                raise ValueError(f"Unknown gee_static_mode: {self.gee_static_mode!r}.")

        if self.needs_gcs and not Path(self.gee_service_account_key).exists():
            raise FileNotFoundError(
                f"GEE/GCS service-account key not found: {self.gee_service_account_key}"
            )

        if not self.ndvi_crop_classes:
            raise ValueError("ndvi_crop_classes must be a non-empty list of class values.")

    def summary(self) -> str:
        lines = [
            f"crop/year/district : {self.crop} / {self.year} / {self.district_name}",
            f"AOI                : {self.aoi_path}"
            + (f"  (from {self.aoi_source})" if self.aoi_source != self.aoi_path else ""),
            f"NDVI source        : {self.ndvi_source}",
            f"static source      : {self.static_source}"
            + (f" ({self.stac_static_mode})" if self.static_source == "stac" else f" ({self.gee_static_mode})")
            + ("" if self.run_static_model else "  [DISABLED: run_static_model=False]"),
            f"NDVI series window : {self.ndvi_series_start} -> {self.ndvi_series_end}",
            f"NDVI inference     : {self.ndvi_inference_start} -> {self.ndvi_inference_end}",
            f"static window      : {self.static_window_start} -> {self.static_window_end}",
            f"NDVI model         : {ModelSource.coerce(self.ndvi_model).describe()}",
            f"static model       : {ModelSource.coerce(self.static_model).describe()}" if self.static_model else "",
            f"model cache        : {self.model_cache_dir}",
        ]
        return "\n".join(line for line in lines if line)


def build_pipeline_config(
    crop: str,
    year: Union[str, int],
    district_name: str,
    aoi_path: Optional[Union[str, Path]] = None,
    **overrides: Any,
) -> PipelineConfig:
    """Builds a fully-resolved PipelineConfig from the crop preset plus any overrides
    (source switches, GEE/STAC options, model sources, path overrides, ...).

    `aoi_path` accepts a local path (str, Path, raw or plain string, any vector format)
    or a `gs://` URI, which is downloaded -- with every shapefile sidecar -- into the AOI
    cache. `aoi_shapefile=` is still accepted as an alias.
    """
    import aoi_io

    year = str(year)
    preset = get_crop_config(crop, year)
    base_dir = overrides.pop("base_dir", "/home/jovyan/FAO")
    aoi_path = aoi_path or overrides.pop("aoi_shapefile", None)
    aoi_gcs_key_path = overrides.pop("aoi_gcs_key_path", None)
    aoi_cache_dir = overrides.pop("aoi_cache_dir", DEFAULT_AOI_CACHE_DIR)
    gee_key_path = overrides.get(
        "gee_service_account_key",
        f"gcs_data_downloader_ee_{overrides.get('gee_project_name', 'farmdar')}.json",
    )

    if aoi_path is None:
        aoi_path = f"{base_dir}/{crop}/all_districts_{crop}/{district_name}/{district_name}.shp"

    aoi_source = aoi_io.normalize_path_text(aoi_path)
    resolved_aoi = aoi_io.resolve_aoi(
        aoi_path, cache_dir=aoi_cache_dir, gcs_key_path=aoi_gcs_key_path or gee_key_path,
    )

    cfg = PipelineConfig(
        crop=crop,
        year=year,
        district_name=district_name,
        aoi_path=str(resolved_aoi),
        aoi_source=aoi_source,
        aoi_gcs_key_path=aoi_gcs_key_path,
        aoi_cache_dir=aoi_cache_dir,
        base_dir=base_dir,
        output_basename=f"{district_name}_{crop}_{year}",
        **preset,
    )

    unknown = [key for key in overrides if not hasattr(cfg, key)]
    if unknown:
        raise TypeError(f"Unknown PipelineConfig field(s): {unknown}")
    for key, value in overrides.items():
        setattr(cfg, key, value)

    if cfg.gcs_base_folder is None:
        cfg.gcs_base_folder = f"fao_{crop}_{year}"
    if cfg.gee_service_account_key is None:
        cfg.gee_service_account_key = f"gcs_data_downloader_ee_{cfg.gee_project_name}.json"

    cfg.ndvi_model = ModelSource.coerce(cfg.ndvi_model)
    if cfg.static_model is not None:
        cfg.static_model = ModelSource.coerce(cfg.static_model)

    return cfg
