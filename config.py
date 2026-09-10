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
            "static_window_end": f"{current_year}-11-25",
            # Tried in order; the first window that yields a clean enough image wins.
            "static_priority_windows": [
                ("11-07", "11-15"),   # best: crop fully developed, dry season begun
                ("11-01", "11-06"),
                ("10-15", "10-31"),
                ("11-16", "11-25"),   # last resort
            ],
            "composite_step_days": 8,
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
            "static_window_start": f"{current_year}-04-20",
            "static_window_end": f"{current_year}-05-20",
            "static_priority_windows": [
                ("05-01", "05-10"),
                ("04-20", "04-30"),
                ("05-11", "05-20"),
            ],
            "composite_step_days": 8,
            "ndvi_model": "gs://farmdar_data_catalog/FAO_SPR_MAIZE_MODELS/FAO_Spr_Maize_NDVI_Model/FAO_Spr_Maize_RF_Model.joblib",
            "static_model": "gs://farmdar_data_catalog/FAO_SPR_MAIZE_MODELS/FAO_Spr_Maize_Static_IMG_Model/FAO_Spr_Maize_XGB_Static_IMG_Model.json",
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
            "static_window_start": f"{current_year}-01-20",
            "static_window_end": f"{current_year}-03-20",
            # Wheat phenology differs by province, so the windows are region-specific.
            "static_priority_windows_by_region": {
                "punjab": [
                    ("02-10", "02-25"),
                    ("01-25", "02-10"),
                    ("02-26", "03-10"),
                    ("03-11", "03-20"),   # last resort
                ],
                "sindh": [
                    ("02-01", "02-20"),
                    ("01-20", "01-31"),
                    ("02-21", "02-29"),   # to end of February; clamped on non-leap years
                ],
            },
            "static_priority_windows": [      # used when no region is given
                ("02-10", "02-25"),
                ("01-25", "02-10"),
                ("02-26", "03-10"),
                ("03-11", "03-20"),
            ],
            "composite_step_days": 8,
            "ndvi_model": "gs://farmdar_data_catalog/FAO_Wheat_Model_Files/FAO_Wheat_NDVI_Model/FAO_Wheat_RF_Model.joblib",
            "static_model": "gs://farmdar_data_catalog/FAO_Wheat_Model_Files/FAO_Wheat_Static_IMG_Model/FAO_Wheat_XGB_Model.json",
        },
        "rice": {
            "ndvi_crop_classes": [1],
            "sieve_min_pixel_size": 20,
            "min_polygon_area_acres": 0.5,
            "static_model_positive_class": 1,
            "static_crop_label": 1,
            "static_background_label": 4,
            "output_polygon_label": 1,
            "ndvi_series_start": f"{current_year}-05-24",
            "ndvi_series_end": f"{current_year}-12-01",
            "ndvi_inference_start": f"{current_year}-06-01",
            "ndvi_inference_end": f"{current_year}-11-30",
            # Rice needs a denser series than the other crops.
            "composite_step_days": 5,
            # No static window: the rice static model does not exist yet, so the static
            # stage is skipped (see PipelineConfig.validate). Fill both in alongside the
            # model when it lands.
            "static_window_start": "",
            "static_window_end": "",
            "ndvi_model": "gs://farmdar_data_catalog/fao_rice_maize_timeseries_model/fao_rice_maize_rf_model.joblib",
            "static_model": None,
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
    region: Optional[str] = None   # e.g. "punjab" / "sindh"; selects region-specific windows
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
    # A date can be cloud-free by metadata yet cover a fraction of the AOI. Below this
    # share of usable AOI the run warns loudly; set "error" to refuse the result.
    stac_static_min_coverage_pct: Optional[float] = 80.0
    stac_static_on_low_coverage: Literal["warn", "error"] = "warn"

    # ------------------------------------------- priority acquisition windows
    # Every configured window is scored, then the earliest (agronomically best) one that
    # clears the coverage floor wins -- unless a later window beats it by more than this
    # many percentage points of AOI coverage, where the cleaner image is worth the
    # slightly worse date. 0 makes coverage the sole criterion; 100 makes window order
    # the sole criterion.
    static_window_preference_margin_pct: float = 5.0
    # A window the catalogue refuses to answer for is not a window without imagery.
    # Scoring is retried, and if a higher-priority window still could not be scored the
    # run stops rather than report the answer from a lower-priority date as if the
    # comparison had been complete. "warn" accepts it instead.
    static_window_score_attempts: int = 3
    static_window_score_retry_seconds: float = 5.0
    static_window_on_score_error: Literal["error", "warn"] = "error"
    # Re-run lever: start from window N instead of the first, for a district whose result
    # from the leading window looked wrong against local knowledge.
    static_window_start_at: int = 1
    # Last resort only, when NO configured window has any usable acquisition: widen the
    # leading window by this many days on each side, up to this many times. Each step is
    # logged as a departure from the crop's phenology. 0 disables it (fail instead).
    static_window_expansion_days: int = 5
    static_window_max_expansions: int = 3

    # ------------------------------------------------- result plausibility check
    # Advisory only -- a run is never failed on these. Crop-share bounds are per-district
    # local knowledge, so they are off until set; the retention bounds catch a static
    # model that either removed nothing or removed almost everything, which is a defect
    # in the imagery or the model rather than a property of the district.
    qc_max_crop_share_pct: Optional[float] = None
    qc_min_crop_share_pct: Optional[float] = None
    # Off by default, and deliberately so. These were once 15.0 / 99.5, fitted to six
    # observations on a single AOI -- a number with no agronomic or published basis,
    # presented to the operator as a verdict. What share of its input mask a static model
    # should keep is a property of the crop, the district and the model, and nothing at
    # run time knows it. `static_retention_pct` is always measured and always reported;
    # set these only if you have the local knowledge to say what is implausible.
    qc_min_static_retention_pct: Optional[float] = None
    qc_max_static_retention_pct: Optional[float] = None
    # The two degenerate outcomes are true without any domain knowledge: keeping none of
    # the mask, and removing none of it. Neither is a plausible crop boundary -- the first
    # is an image or model that produced nothing, the second a mask that never applied.
    # This is the tolerance for "none"/"all", not a plausible-range bound. 0 means exactly
    # zero and exactly full, and is the default because any other value is a number
    # somebody chose. The cost is real and worth knowing: a near-collapse -- the static
    # model keeping 0.3% of its mask -- is now reported and not warned about. Raise this
    # only against evidence, not against intuition.
    qc_degenerate_retention_tolerance_pct: float = 0.0
    # Per-tile cost, now that the figure is real per-tile time rather than throughput.
    # Healthy tiles have been measured at ~2.4 min each; the credential-refresh and 502
    # failures reported from the field ran 15-30 min. 8 sits clear of both.
    stac_slow_tile_warning_minutes: float = 8.0
    stac_resolution_m: int = 10
    stac_tile_size_deg: float = 0.1
    stac_worker_count: int = 8
    # STAC acquisition holds every in-flight tile in memory, so peak tracks
    # (tiles in flight) x (tile area) and is independent of AOI size. Measured at
    # ~1.46 GiB per in-flight tile at tile_deg=0.1; raising tile_deg scales it by the
    # square. The pipeline estimates the peak before acquiring and trims the worker
    # count to fit this fraction of free RAM.
    stac_tile_memory_gib: float = 1.46
    stac_memory_fraction: float = 0.6

    # -------------------------------------------------------------- GEE options
    gee_project_name: str = "farmdar"
    gee_service_account_key: Optional[str] = None  # default: gcs_data_downloader_ee_{gee_project_name}.json
    gcs_bucket: str = "farmdar_data_catalog"
    gcs_base_folder: Optional[str] = None  # default: fao_{crop}_{year}
    gee_grid_cell_acres: int = 15000
    gee_landsat_cutover_year: int = 2018  # year < this -> Landsat 8 only (never Landsat 7)
    # Sentinel-2 archive start. Below this there is no Sentinel-2 static image to be had
    # from either backend, and the static models cannot use Landsat (see uses_landsat_static).
    sentinel2_start_year: int = 2016
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
    # Each static worker holds its own copy of the model, so the pool is capped by
    # memory as well as by cores: workers x (model size x expansion) must fit inside
    # this fraction of free RAM. A 563 MB gradient-boosted JSON was measured at 5.2 GiB
    # resident, so sizing purely from cpu_count() reserves tens of GiB of copies.
    static_memory_fraction: float = 0.5
    static_model_memory_expansion: float = 12.0
    ndvi_tile_timeout_s: int = 900

    # ------------------------------------------------ classification thresholds
    ndvi_crop_classes: List[int] = field(default_factory=list)
    # Sieve thresholds are resolution-dependent: a 20-pixel blob at Sentinel's 10 m is
    # ~0.5 acre, but at Landsat's 30 m it would discard real fields, so Landsat runs use
    # a much smaller threshold. Read these through the *_sieve_min_pixels properties.
    sieve_min_pixel_size: int = 20
    sieve_min_pixel_size_landsat: int = 1
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
    # Acquisition windows tried in preference order, as ("MM-DD", "MM-DD") pairs. The
    # first that yields an image clean enough for the coverage floor wins, so the
    # pipeline reaches for the phenologically best imagery before settling.
    static_priority_windows: List[tuple] = field(default_factory=list)
    static_priority_windows_by_region: dict = field(default_factory=dict)

    # ----------------------------------------------------------------- outputs
    export_shapefile_zip: bool = True
    dissolve_polygons: bool = False  # see postprocess.vectorize_process_and_export

    # ------------------------------------------------------- local-disk cleanup
    delete_raw_ndvi_tiles: bool = True
    delete_raw_static_tiles: bool = True

    # ------------------------------------------------------------- run control
    # Every stage writes into its own numbered folder (`1_ndvi_run_2`, ...), so the same
    # AOI and year can be run repeatedly without renaming anything by hand.
    #   "new"    -> a clean folder, nothing reused
    #   "resume" -> continue the latest run, reusing whatever finished
    #   "3"      -> a specific run id
    # The per-stage settings override `run_mode` when set, which is how you resume an
    # expensive NDVI stage while forcing a fresh static stage.
    run_mode: str = "resume"
    ndvi_run_mode: Optional[str] = None
    static_run_mode: Optional[str] = None
    vector_run_mode: Optional[str] = None
    run_tag: Optional[str] = None  # optional label appended to new folder names

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
    def ndvi_resolution_m(self) -> int:
        return self.stac_resolution_m if self.ndvi_source == "stac" else self.gee_resolution_m

    @property
    def static_resolution_m(self) -> int:
        return self.stac_resolution_m if self.static_source == "stac" else self.gee_resolution_m

    def _sieve_min_pixels(self, resolution_m: int) -> int:
        return self.sieve_min_pixel_size_landsat if resolution_m >= 30 else self.sieve_min_pixel_size

    @property
    def ndvi_sieve_min_pixels(self) -> int:
        return self._sieve_min_pixels(self.ndvi_resolution_m)

    @property
    def static_sieve_min_pixels(self) -> int:
        return self._sieve_min_pixels(self.static_resolution_m)

    def resolved_static_windows(self) -> List[tuple]:
        """Concrete (start, end) date pairs for this year, in preference order."""
        import calendar

        windows = self.static_priority_windows
        if self.region and self.static_priority_windows_by_region:
            regional = self.static_priority_windows_by_region.get(self.region.strip().lower())
            if regional:
                windows = regional
            else:
                known = sorted(self.static_priority_windows_by_region)
                raise ValueError(
                    f"No static windows defined for region {self.region!r} on {self.crop}. "
                    f"Known regions: {known}."
                )

        year = int(self.year)
        resolved = []
        for start_md, end_md in windows:
            start_month, start_day = (int(part) for part in start_md.split("-"))
            end_month, end_day = (int(part) for part in end_md.split("-"))
            # Clamp to the month's real length so "02-29" works in a non-leap year.
            end_day = min(end_day, calendar.monthrange(year, end_month)[1])
            resolved.append((
                f"{year}-{start_month:02d}-{start_day:02d}",
                f"{year}-{end_month:02d}-{end_day:02d}",
            ))
        return resolved

    @property
    def sentinel2_available(self) -> bool:
        return int(self.year) >= self.sentinel2_start_year

    @property
    def uses_landsat_static(self) -> bool:
        """True when the static image would come from Landsat 8 rather than Sentinel-2.

        Only the GEE backend can produce a pre-Sentinel-2 static image; STAC is
        Sentinel-2 throughout, so a pre-cutover STAC static image is still Sentinel-2.
        """
        return self.static_source == "gee" and not self.uses_sentinel

    @property
    def has_static_model(self) -> bool:
        return self.static_model is not None

    def stage_mode(self, stage_field: Optional[str]) -> str:
        return stage_field if stage_field else self.run_mode

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

        if self.run_static_model and not self.sentinel2_available:
            raise ValueError(
                f"run_static_model=True for {self.year}, but the static models need a "
                f"Sentinel-2 image and the archive starts {self.sentinel2_start_year}. "
                "Set run_static_model=False; the NDVI-only product is the deliverable for "
                "pre-Sentinel-2 years."
            )

        if self.run_static_model and self.uses_landsat_static:
            raise ValueError(
                f"run_static_model=True with a Landsat static image ({self.year} < "
                f"gee_landsat_cutover_year={self.gee_landsat_cutover_year}). The static "
                "models are trained on Sentinel-2's real red-edge band; Landsat 8 has no "
                "red-edge, so gee_client.homogenize_landsat8 substitutes (red + NIR) / 2 "
                "and the model receives a feature that behaves nothing like the one it "
                "learned -- measured 77x below the Sentinel-2 result on the same AOI. "
                "Set run_static_model=False for Landsat years."
            )

        if self.run_static_model:
            if not self.has_static_model:
                raise ValueError(
                    f"run_static_model=True but no static model is configured for '{self.crop}'. "
                    "Set run_static_model=False, or point static_model at a model file."
                )
            if not (self.static_window_start and self.static_window_end):
                raise ValueError(
                    f"run_static_model=True but '{self.crop}' has no static acquisition window. "
                    "Set static_window_start / static_window_end."
                )
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

        import logging

        if self.ndvi_source == "gee" and self.sentinel2_available:
            logging.getLogger(__name__).warning(
                f"ndvi_source='gee' for {self.year}, a year Sentinel-2 covers. GEE NDVI was "
                "measured 2.9x slower than STAC (99.8% of samples below 30% CPU, blocked on "
                "the export queue) for a product 0.2% different (IoU 0.947). It is kept as a "
                "fallback; prefer ndvi_source='stac' when Sentinel-2 is available."
            )
        elif self.ndvi_source == "stac" and not self.sentinel2_available:
            raise ValueError(
                f"ndvi_source='stac' but Sentinel-2 does not cover {self.year} "
                f"(archive starts {self.sentinel2_start_year}). Use ndvi_source='gee', which "
                "falls back to Landsat 8 for pre-Sentinel-2 years."
            )

        for label, mode in (
            ("run_mode", self.run_mode),
            ("ndvi_run_mode", self.ndvi_run_mode),
            ("static_run_mode", self.static_run_mode),
            ("vector_run_mode", self.vector_run_mode),
        ):
            if mode is None:
                continue
            text = str(mode).strip().lower()
            if text not in ("new", "resume", "resume_latest", "latest", "continue") and not text.isdigit():
                raise ValueError(
                    f"{label}={mode!r} is not valid. Use 'new', 'resume', or a run id like '3'."
                )

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
            (f"static model       : {ModelSource.coerce(self.static_model).describe()}"
             if self.static_model else f"static model       : (none yet for {self.crop})"),
            f"model cache        : {self.model_cache_dir}",
            f"run modes          : ndvi={self.stage_mode(self.ndvi_run_mode)}, "
            f"static={self.stage_mode(self.static_run_mode)}, "
            f"vector={self.stage_mode(self.vector_run_mode)}"
            + (f", tag={self.run_tag}" if self.run_tag else ""),
            f"sieve min pixels   : ndvi={self.ndvi_sieve_min_pixels}, static={self.static_sieve_min_pixels}",
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
    # `pop` unconditionally: `aoi_path or overrides.pop(...)` short-circuits when
    # aoi_path is set, leaving the alias in overrides to be reported as an unknown field.
    aoi_alias = overrides.pop("aoi_shapefile", None)
    if aoi_path is not None and aoi_alias is not None:
        raise TypeError(
            "Pass either aoi_path or its alias aoi_shapefile, not both "
            f"(aoi_path={aoi_path!r}, aoi_shapefile={aoi_alias!r})."
        )
    aoi_path = aoi_path or aoi_alias
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

    if int(cfg.year) < cfg.sentinel2_start_year and "ndvi_source" not in overrides:
        # STAC is Sentinel-2 only, so pre-archive years must use GEE's Landsat 8 path.
        import logging

        logging.getLogger(__name__).info(
            f"{cfg.crop} {cfg.year}: before Sentinel-2, so NDVI comes from GEE/Landsat 8."
        )
        cfg.ndvi_source = "gee"

    if cfg.gcs_base_folder is None:
        cfg.gcs_base_folder = f"fao_{crop}_{year}"
    if cfg.gee_service_account_key is None:
        cfg.gee_service_account_key = f"gcs_data_downloader_ee_{cfg.gee_project_name}.json"

    cfg.ndvi_model = ModelSource.coerce(cfg.ndvi_model)
    if cfg.static_model is not None:
        cfg.static_model = ModelSource.coerce(cfg.static_model)
    elif "run_static_model" not in overrides:
        # No static model exists for this crop yet (rice), so default to the NDVI-only
        # product instead of failing. An explicit run_static_model=True still errors in
        # validate(), rather than silently doing nothing.
        cfg.run_static_model = False

    if cfg.run_static_model and not cfg.sentinel2_available and "run_static_model" not in overrides:
        import logging

        logging.getLogger(__name__).warning(
            f"{cfg.crop} {cfg.year}: no Sentinel-2 static image exists before "
            f"{cfg.sentinel2_start_year} -- running NDVI-only."
        )
        cfg.run_static_model = False

    if cfg.run_static_model and cfg.uses_landsat_static and "run_static_model" not in overrides:
        # Landsat static imagery cannot feed a Sentinel-2-trained static model (see
        # validate). Fall back to the NDVI-only product so a multi-year batch keeps
        # running; an explicit run_static_model=True still raises.
        import logging

        logging.getLogger(__name__).warning(
            f"{cfg.crop} {cfg.year}: static image would come from Landsat 8, which the "
            "static model cannot use -- running NDVI-only."
        )
        cfg.run_static_model = False

    return cfg
