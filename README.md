# FAO Crop Mapping Pipeline

Crop acreage mapping for sugarcane, wheat, spring maize and rice from Sentinel-2 /
Landsat 8 imagery, with **independently
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

### Which backend for what

Validated on real AOIs, so these are measurements rather than preferences:

- **NDVI comes from STAC.** The two backends produce the same product (0.2% apart,
  IoU 0.947), but GEE NDVI took 2.9× the wall clock, spending 99.8% of samples below
  30% CPU blocked on the export queue. `ndvi_source="gee"` still works and warns.
- **GEE is for the static image**, where it is ~60× cheaper on network than STAC
  (40 MB vs 3–5 GB) because compositing happens server-side.
- **No static model on Landsat.** The static models are trained on Sentinel-2's real
  red-edge band; Landsat 8 has none, so `homogenize_landsat8` substitutes
  `(red + NIR) / 2` and the model gets a feature unlike anything it learned — measured
  77× below the Sentinel-2 result on the same AOI. Pre-cutover years with
  `static_source="gee"` therefore run NDVI-only automatically; forcing
  `run_static_model=True` raises. A pre-2018 **STAC** static image is still Sentinel-2
  and remains supported.

### Static acquisition windows

A single wide date range lets the selector pick anything inside it, and a scene that
looks perfect by `eo:cloud_cover` can still be a swath edge covering 15% of the AOI, or
hazy enough to shift the DN values the static model keys on. Each crop therefore defines
**windows in preference order**, scored against farmdar's selector (a coarse scan, no
imagery downloaded) until one clears `stac_static_min_coverage_pct`:

| Crop | Windows, best first |
|---|---|
| cane | 7–15 Nov · 1–6 Nov · 15–31 Oct · 16–25 Nov |
| wheat (punjab) | 10–25 Feb · 25 Jan–10 Feb · 26 Feb–10 Mar · 11–20 Mar |
| wheat (sindh) | 1–20 Feb · 20–31 Jan · 21 Feb–end Feb |
| spr_maize | 1–10 May · 20–30 Apr · 11–20 May |

Pass `region="punjab"` / `region="sindh"` for wheat; without it the Punjab schedule is
used. February end dates are leap-year aware. If no window clears the floor the best
one is used and the run says so loudly. `stac_static_mode="manual"` bypasses all of it.

Haze defence is `cloud_metric="aoi"` (the default): it scores against the SCL band,
which classes cirrus alongside cloud, rather than trusting scene-level `eo:cloud_cover`.

### Sensor eras

| Year | NDVI | Static |
|---|---|---|
| ≥ 2016 | STAC (Sentinel-2) | Sentinel-2, either backend |
| 2014–2015 | GEE (Landsat 8), selected automatically | none — no Sentinel-2 to use |

`sentinel2_start_year` (default 2016) draws the line. Below it, `ndvi_source` switches to
GEE automatically, `ndvi_source="stac"` is refused with the reason, and the static stage
turns itself off. GEE NDVI is kept for Sentinel-2 years too, as a fallback, and warns.

### Cost to budget

- Pre-2018 STAC years cost roughly 8× the network and 2.4× the time of a recent year
  (measured 40 GB / 16 min vs 5 GB / 7 min for the same AOI), because older Sentinel-2
  reprocessings are less range-request friendly.
- Acquisition is ~89% of wall clock. Peak memory tracks *tiles in flight × tile area*,
  not AOI size, so raising `stac_tile_size_deg` must be paired with lowering
  `stac_worker_count` — the pipeline estimates this before acquiring and trims the
  worker count to fit `stac_memory_fraction` of free RAM.

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
| `run_manager.py` | numbered run folders, new/resume per stage |

This supersedes three earlier per-source notebooks (GEE-only, STAC-only, and a
static-model-only workaround), which are not part of this repository.

## Run folders: new vs resume

Every stage writes into its own numbered folder under the output directory:

```
<output>/1_ndvi_run_2/      raw tiles, tile predictions, classification map, sieved map
<output>/2_static_run_1/    static imagery, classified raster, sieved raster
<output>/3_vector_run_2/    final GPKG / zipped Shapefile
```

`run_mode` decides which number a stage uses, so the same AOI and year can be run
repeatedly without renaming anything by hand:

| Value | Effect |
|---|---|
| `"resume"` (default) | Continue the latest run, reusing whatever already finished — the way to pick up after a crash |
| `"new"` | A clean folder (`max + 1`), nothing reused |
| `"2"` | A specific run id |

`ndvi_run_mode`, `static_run_mode` and `vector_run_mode` override `run_mode` per stage —
so you can keep an expensive NDVI result and redo only the static stage:

```python
cfg = build_pipeline_config(
    crop="cane", year="2025", district_name="aoi_11", aoi_path=...,
    ndvi_run_mode="resume",     # keep the NDVI work
    static_run_mode="new",      # redo the static image from scratch
)
```

`run_tag="cloudfix"` appends a label to new folder names. Each stage folder carries a
`run_info.json` recording what produced it, appending to a history across attempts.

## Configuration notes

**Disk.** `delete_raw_ndvi_tiles` and `delete_raw_static_tiles` (both `True` by default)
control whether raw acquired imagery is removed once consumed. Set them `False` to keep
tiles for debugging or to avoid re-downloading.

**Memory.** `ndvi_worker_count` (default 75% of cores), `ndvi_worker_max_tasks`
(recycles a worker after N tiles; `1` is the most memory-safe), `static_worker_count`
and `static_chunk_size` bound how much runs at once.

**Sieve thresholds are resolution-aware.** `sieve_min_pixel_size` (20) applies at
Sentinel's 10 m; Landsat runs at 30 m use `sieve_min_pixel_size_landsat` (1), since 20
pixels there would discard real fields. Each stage is sized by its own source, so a
STAC-NDVI + GEE-Landsat-static run gets 20 and 1 respectively.

**Rice** has no static model yet, so `run_static_model` defaults to `False` for it and
the pipeline delivers the NDVI-only product. Setting it `True` raises a clear error
rather than silently doing nothing. It also uses a 5-day compositing step where the
other crops use 8.

**Class labels** live in three distinct spaces, which is easy to confuse:

- `ndvi_crop_classes` — values in the NDVI/RF map meaning "crop"; also gate the static model.
- `static_model_positive_class` / `static_crop_label` / `static_background_label` — the
  XGBoost model's raw positive class, the label written for it, and everything else.
- `output_polygon_label` — the label stamped on exported polygons.
