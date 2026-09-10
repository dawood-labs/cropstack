# FAO Crop Mapping Pipeline

Crop acreage mapping from Sentinel-2 / Landsat 8 imagery, with **independently
switchable data sources**: NDVI time series and the static image can each come from
Google Earth Engine or from an open STAC catalogue, in any combination.

Two models run in sequence:

1. **NDVI time series → RandomForest.** Red/NIR composites every 8 days across the
   season, Whittaker-smoothed per pixel, classified into a crop map.
2. **Static single-date image → XGBoost.** A cloud-free composite classified only
   where step 1 already found the crop, then sieved and vectorised to polygons.

## Why the source switch exists

The pipeline began as GEE-only. When GEE stopped being an option for the client, NDVI
acquisition moved to a STAC catalogue — but the static image was harder: cloud cover
kept ruining it, so the workaround became "pick a scene by eye in the GEE Code Editor,
export it to GCS by hand, and point the pipeline at that file". All three paths are now
first-class modes you select in config, rather than three diverging notebooks.

## Install

```bash
pip install -r requirements.txt
```

GDAL's Python bindings (`osgeo`) are optional but recommended — they enable streaming
mosaics, which keep memory flat on district-sized rasters. Install via conda-forge.

STAC acquisition uses `farmdar.sentinel`, which is **synced read-only from the farmdar
repo and must never be edited here**. Every fix in this project is achieved through how
it is called.

## Quick start

```python
from config import build_pipeline_config
from pipeline import run_pipeline

cfg = build_pipeline_config(
    crop="cane", year="2025", district_name="Muzaffargarh",
    ndvi_source="stac",        # 'gee' | 'stac'
    static_source="stac",      # 'stac' | 'gee'
    stac_static_mode="auto",   # cloud-aware date selection
)
result = run_pipeline(cfg)
```

Or open [`4_fao_unified_pipeline.ipynb`](4_fao_unified_pipeline.ipynb) and edit the
config cell.

## Source combinations

| `ndvi_source` | `static_source` | sub-mode | Use case |
|---|---|---|---|
| `stac` | `stac` | `stac_static_mode="auto"` | Fully open-source, no GEE (default) |
| `stac` | `stac` | `stac_static_mode="manual"` | You supply the static date(s) |
| `stac` | `gee` | `gee_static_mode="manual_gcs_link"` | Scene you exported by hand from the GEE Code Editor |
| `stac` | `gee` | `gee_static_mode="api_auto"` | GEE picks and exports the composite by code |
| `stac` | `gee` | `gee_static_mode="api_manual"` | You name the date, GEE exports it by code |
| `gee` | `stac` / `gee` | any | GEE NDVI; years before `gee_landsat_cutover_year` use Landsat 8 |

Landsat 7 is never used — its SLC-off failure leaves permanent stripes on every scene.

## AOI input

`aoi_path` takes any of these — local or GCS, in any common vector format:

```python
aoi_path = r"C:\data\aoi\fao_cane_validation_aoi_11.shp"   # raw string (recommended on Windows)
aoi_path = "/home/jovyan/FAO/cane/aoi_11.gpkg"
aoi_path = Path("/home/jovyan/FAO/cane/aoi.geojson")
aoi_path = "gs://bucket/aois/aoi_11.shp"                   # sidecars fetched automatically
aoi_path = "gs://bucket/aois/districts.gpkg"
```

Formats: `.shp` (sidecars handled), `.gpkg`, `.geojson`/`.json`, `.kml`, `.fgb`,
`.parquet`, or a `.zip` containing any of them. GCS AOIs are cached under
`~/.cache/fao_pipeline/aoi`; add `aoi_gcs_key_path` if the bucket needs different
credentials. Surrounding quotes and stray whitespace are stripped, and the AOI is
resolved and checked when the config is built — a bad path fails immediately.

> **Windows vs WSL.** The same file is spelled `C:\data\aoi.shp` on Windows,
> `/mnt/c/data/aoi.shp` under WSL, and `/c/data/aoi.shp` under Git Bash. Notebooks are
> often written on one and executed on the other, so all three spellings are tried
> automatically — a Windows path passed to code running under WSL just works. This
> applies to model `local_path`s too.

> **Windows paths:** prefer `r"C:\path\to\aoi.shp"`. Without the `r`, Python eats the
> backslash escapes *before* the pipeline sees the string — `\fao_...` becomes a
> formfeed character. The resolver detects and repairs this (with a warning), but the
> raw string avoids relying on it.

## Models

`ndvi_model` and `static_model` each accept three forms:

```python
ndvi_model = "/home/jovyan/FAO/cane/model_files/best_rf_classifier.joblib"  # local path
ndvi_model = "gs://bucket/models/best_rf_classifier.joblib"                 # cached after first download
ndvi_model = {"gcs_uri": "gs://other-bucket/model.joblib",                  # its own credentials
              "gcs_key_path": "/path/to/other_sa_key.json"}
```

A `gs://` model is downloaded once into `model_cache_dir`
(default `~/.cache/fao_pipeline/models`, mirroring the bucket layout) and read from
there on every later run. Models resolve before any imagery is fetched, so a bad path
fails in seconds rather than an hour in.

## Batch

```bash
python batch.py --jobs jobs.example.json --results batch_results.csv
```

Jobs run sequentially (each already uses every core), Earth Engine initialises once for
the whole batch, and a failing job is recorded and skipped rather than killing the run.
From Python:

```python
from batch import build_jobs, run_batch

jobs = build_jobs(
    crop="cane", year="2025",
    districts=["aoi_0", "aoi_1", "aoi_2"],
    # {district_name}, {crop} and {year} are filled per district.
    # Use a PLAIN string -- an f-string would interpolate before the districts exist.
    aoi_path=r"C:\data\split_aoi_folders\{district_name}\{district_name}.shp",
)
results = run_batch(jobs, results_csv="batch_results.csv")
```

When the AOIs follow no pattern, pass `aoi_paths=[...]` instead — one entry per
district, in the same order (local paths or `gs://` URIs).

## Layout

| Module | Role |
|---|---|
| `config.py` | `PipelineConfig`, crop presets, `ModelSource`, validation |
| `pipeline.py` | `run_pipeline(cfg)` — the whole thing end to end |
| `batch.py` | many districts / crops / years, plus a CLI |
| `ndvi_pipeline.py` | NDVI stage: source dispatch → inference → mosaic → sieve |
| `static_pipeline.py` | static stage: source dispatch → mask → classify → sieve |
| `gee_client.py` | GEE acquisition: grid split, asset ingest, exports |
| `gcs_io.py` | GCS downloads, including GEE's sharded exports |
| `raster_io.py` | streaming (low-memory) mosaicking |
| `inference_workers.py` | Whittaker smoothing + RandomForest inference |
| `static_classify.py` | XGBoost static classification + crop masking |
| `postprocess.py` | sieve + vectorise/export |
| `band_utils.py` | band parsing across both naming conventions |
| `aoi_io.py` | AOI resolution: any path form, any vector format, local or GCS |
| `model_registry.py` | model resolution + permanent download cache |

This supersedes three earlier per-source notebooks (GEE-only, STAC-only, and a
static-model-only workaround), which are not part of this repository.

## Configuration notes

**Disk.** `delete_raw_ndvi_tiles` and `delete_raw_static_tiles` (both `True` by default)
control whether raw acquired imagery is removed once consumed. Set them `False` to keep
tiles for debugging or to avoid re-downloading.

**Memory.** `ndvi_worker_count` (default 75% of cores), `ndvi_worker_max_tasks`
(recycles a worker after N tiles; `1` is the most memory-safe), `static_worker_count`
and `static_chunk_size` bound how much runs at once.

**Class labels** live in three distinct spaces, which is easy to confuse:

- `ndvi_crop_classes` — values in the NDVI/RF map meaning "crop"; also gate the static model.
- `static_model_positive_class` / `static_crop_label` / `static_background_label` — the
  XGBoost model's raw positive class, the label written for it, and everything else.
- `output_polygon_label` — the label stamped on exported polygons.
