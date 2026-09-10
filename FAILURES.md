# FAILURES.md — every defect found, with reproduction

Environment: 8 vCPU / 61 GiB Intel Xeon 6975P (KVM), Python 3.11.15, GDAL 3.13.2
(`osgeo` present, so mosaics stream via VRT), rasterio 1.4.3, geopandas 1.0.1,
xgboost 3.2.0, scikit-learn 1.6.1. cropstack @ `b013476`. AOI: `okara_test_data_*.shp`,
51,293 acres (all four crop AOIs are byte-identical, md5 `883becff…`).

**33 of 34 instrumented scenarios exited 0.** The single non-zero exit is FAIL-9, and
it is a late, opaque failure rather than a clean rejection. Every *other* defect below is
a silent one: the pipeline exits 0 and writes a plausible-looking product that is wrong.
That is the central risk in this codebase right now.

| # | Sev | Defect | Silent? |
|---|---|---|---|
| FAIL-1 | HIGH | Static-stage sieve is a no-op | yes |
| FAIL-2 | HIGH | `run_mode="resume"` returns a stale vector product | yes |
| FAIL-3 | HIGH | A 16%-footprint static scene is accepted, and as anchor corrupts the rest | yes |
| FAIL-6 | HIGH | An 83%-cloud static image is classified with no gate | yes |
| FAIL-7 | HIGH | `static_worker_count` ignores model size — 27.9 GiB peak, OOM path at scale | yes |
| FAIL-8 | HIGH | Pre-2018 GEE/Landsat static path returns an implausible result | yes |
| FAIL-4 | MED | `.parquet` AOIs are advertised but never work | no (raises) |
| FAIL-5 | MED | A resumed static run folder accumulates unrelated products | yes |
| FAIL-9 | MED | `api_manual` accepts a date with no scene, fails opaquely minutes later | no (raises) |
| OBS-6 | LOW | Misleading error when both AOI spellings are passed | no (raises) |
| OBS-7 | LOW | Degenerate-crop-mask guard measures against the wrong denominator | yes |

---

## FAIL-1 (HIGH) — The static-stage sieve is a no-op

**What happens.** `static_pipeline.run_static_pipeline` finishes with:

```python
return postprocess.apply_strict_directional_sieve(
    input_raster_path=str(classified_path),
    target_classes=[cfg.static_crop_label],       # [1]
    min_pixel_size=cfg.static_sieve_min_pixels,   # 20
    connectivity=4,
    nodata_val=cfg.static_background_label,       # 4  <-- the problem
)
```

Inside the sieve, `valid_mask = data != nodata_val` masks out **every background
pixel** — 97.7% of the raster. `rasterio.features.sieve` replaces a small blob with its
largest *valid* neighbour, and a small class-1 blob surrounded entirely by masked-out
class-4 has no valid neighbour, so it can never be replaced. Nothing is ever removed.

The NDVI stage passes `nodata_val=255`, a value no pixel actually holds, so its mask is
all-True and its sieve works normally. The two stages give the same function opposite
meanings for the same argument.

**Evidence** (`A1_cane_2025`, `static_mosaic_16_Oct_2025_Cls.tif`):

| | class-1 pixels |
|---|---|
| before sieve | 115,777 |
| after sieve, **as the pipeline calls it** | 115,777 (delta 0) |
| after sieve, same call without the mask | 106,793 (delta −8,984) |

8,984 pixels ≈ 222 acres ≈ 7.8% of the class should have been removed. For comparison
the NDVI sieve removed 58,185 px (−19.2%).

The output is still written as `..._Cls_sieved_p20.tif`, so the no-op is invisible from
the filename, the logs, or the return value.

**Reproduction:** `harness/sieve_probe.py` (self-contained; reads a finished run's
`_Cls.tif`).

**Downstream effect.** `min_polygon_area_acres=0.5` (≈20 px at 10 m) removes much of the
same speckle later, which is why the final GPKG is not garbage — but the strict
encapsulation-revert logic is skipped entirely, and the leftover fragmentation is
visible: `A2_cane_2025` keeps only 0.49× its own raster's in-AOI crop area, versus
0.86–0.87× for the runs whose classification is cleaner.

---

## FAIL-2 (HIGH) — `run_mode="resume"` returns a stale vector product

**What happens.** `postprocess.vectorize_process_and_export` short-circuits on a pure
filename test:

```python
if Path(gpkg_output).exists() and (not save_shp_zip or Path(zip_output).exists()):
    print(f"[Skipped] Vectorised outputs already exist for: {output_basename}")
    return gpkg_output
```

Nothing records which raster produced that file. With `run_mode="resume"` — **the
default** — a re-run that recomputes the static stage still resumes the same vector
folder and hands back the previous run's polygons.

**Evidence.** One workspace, `runs/A1_cane_2025`, same AOI, same year, NDVI stage
resumed bit-identically throughout:

| run | static raster it produced | acres in that raster | GPKG returned |
|---|---|---|---|
| `A1_cane_2025` | `16_Oct_2025` | 2,860.9 | 2,120.3 ✓ |
| `A2_cane_2025` | `18_Oct_2025_and_10_Nov_2025` | 1,118.9 | 465.5 ✓ |
| **`C2_second_resume`** | `16_Oct_2025` | **2,860.9** | **465.5 ✗** |
| `C6` (`vector_run_mode="new"`) | `16_Oct_2025` | 2,860.9 | 2,120.3 ✓ |

`C2_second_resume` exits 0. Its `run_pipeline` return dict reports
`sieved_static_raster` = its own freshly-computed 16-Oct raster and `vector_output` =
A2's polygons from a completely different static image. The correct vector product for
that raster is 2,120.3 acres — `A1`, `C6` and `X5` all produce exactly that from the same
16-Oct image — so the returned 465.5 acres is **4.6× low**. Measured the other way, its
own raster clipped to the AOI holds 2,437.0 acres of crop against the GPKG's 465.5, a
ratio of 0.19. Either way, nothing in the logs indicates it.

**Config that triggers it:** any second `run_pipeline` call on a workspace that already
has a vector run, with default `run_mode="resume"`, where anything upstream changed.

**The fix already exists elsewhere in this codebase.** The NDVI stage checkpoints on
content, not on filenames: predictions are written to `.tmp.tif` and renamed only once
complete, so a SIGKILL mid-inference leaves nothing that can be mistaken for finished
work. Verified directly — see `TEST_REPORT.md` OK-7, where a killed run resumed to a
byte-identical product. Apply the same discipline here: key the vector short-circuit on
the source raster's identity (path + mtime, or a hash recorded in `run_info.json`).

**Minimal reproduction:**
```bash
python harness/run_scenario.py --name r1 --spec specs/A2_cane_2025.json  # writes 3_vector_run_N
python harness/run_scenario.py --name r2 --spec specs/C2_second_resume.json
python harness/stale_vector_probe.py
```

**Contributing factor.** The vector stage's `run_info.json` records neither `resumed`
nor `source` (both come out `null`), so the history cannot show that a run reused a
file rather than producing one.

---

## FAIL-3 (HIGH) — A static scene covering 16% of the AOI is accepted silently, and as the anchor it corrupts the rest

> **Correction to an earlier draft of this file.** I first reported that the STAC path
> feeds the models un-offset baseline-04.00 reflectance (+1000 DN, NDVI −35%) while GEE
> harmonises. **That was wrong and is withdrawn.** The measurement behind it read the
> Planetary Computer directly with `odc.stac.load()`, bypassing `farmdar.sentinel` —
> which handles the offset deliberately and correctly (`harmonize_offset: 1000` for MPC,
> `0` for Earth Search, gated on `s2:processing_baseline >= 4.0`, with an explicit "GEE
> parity" note at `sentinel.py:212`). The controlled experiment below shows the two
> backends agreeing to **0.5%** on the same date. The real defect is different.

**The controlled experiment.** Identical AOI, identical resumed NDVI stage, identical
XGBoost model; only the static image varies. Single-date runs isolate date from backend:

| scenario | static image | raster acres | features | final GPKG acres |
|---|---|---|---|---|
| `X5_stac_16oct_only` | STAC 2025-10-16 | 2,860.9 | 932 | 2,120.3 |
| `X3_stac_18oct_only` | STAC 2025-10-18 | 520.0 | 145 | **365.6** |
| `X4_stac_10nov_only` | STAC 2025-11-10 | 2,480.5 | 795 | 1,817.9 |
| `A4_cane_2025` | **GEE** 2025-11-10 | 2,471.1 | 788 | 1,808.1 |
| `A2_cane_2025` | STAC `[10-18, 11-10]` | 1,118.9 | 247 | **465.5** |

**(a) The backends agree.** Same date, different backend: 1,817.9 vs 1,808.1 acres —
0.5% apart, 795 vs 788 features.

**(b) 2025-10-18 barely touches the AOI, and nothing checks.**

| date | platform | rel. orbit | granule nodata | **AOI covered by footprint** |
|---|---|---|---|---|
| 2025-10-16 | S2B | 48 | 0.10% | 100.00% |
| **2025-10-18** | **S2C** | **5** | **55.09%** | **15.73%** |
| 2025-11-10 | S2C | 48 | 0.18% | 100.00% |

2025-10-18 is a swath-edge acquisition from a different relative orbit. Its
`eo:cloud_cover` is 0.009% — flawless by the metric a human would check — yet it covers
15.7% of the AOI. Used alone, the pipeline maps that sliver, reports 365.6 acres as a
finished product, and warns about nothing. Pixels with no data are written as
`static_background_label`, so the output cannot distinguish "not crop" from "never
imaged".

**(c) As the anchor it also corrupts the 84% that *was* imaged.** In
`stac_static_dates` the **first element becomes the anchor**: farmdar layers it on top
*and* uses it as the radiometric reference for `match_layers="median"` — the default
that `_acquire_static_from_stac` never overrides. So the rest of the image is
brightness-shifted to match a 15.7% sliver, and the composite scores worse than either
date alone:

```
pure 2025-11-10                    1,817.9 acres
pure 2025-10-18                      365.6 acres
[10-18 anchor, 11-10 filling]        465.5 acres   <- worse than its own 84% majority
naive spatial mixing would predict  ~1,590 acres
```

Band statistics corroborate: the mixed composite reads red 1011 / NIR 3051 against the
pure 2025-11-10 image's red 1178 / NIR 2314 — a radiometric shift 15.7% of spatial
mixing cannot produce.

Writing dates chronologically — the natural thing to do — is exactly what promotes the
older, partial scene to anchor. `config.py` documents `stac_static_dates` only as
`['YYYY-MM-DD', ...]`, with no mention that order chooses the anchor. The GEE mode names
its layers explicitly (`gee_static_top_date` / `gee_static_bottom_date`), so the same
pair of dates written the way each mode expects yields *different* images — and only the
GEE spelling makes the priority visible.

**The same blind spot in another guise.** `A1_cane_2016` logged
`static: dates=['2016-11-14'] (anchor=2016-11-14) -> 17.15% of AOI usable` and carried
on regardless. `_acquire_static_from_stac` passes `mask_clouds=False`, so the image comes
back complete but ~83% cloud and XGBoost classifies the cloud as ground. cropstack reads
`result["selection"]["coverage_pct"]` only to log it.

**Fix.** (1) Gate on coverage: refuse, or warn loudly, when a selected static date's
footprint or cloud-free fraction covers less than a configurable share of the AOI — the
metadata (`s2:nodata_pixel_percentage`, the item footprint, the selector's own
`coverage_pct`) is already in hand, and `build_crop_mask` sets the precedent with its
degenerate-mask assertion. (2) Document that `stac_static_dates[0]` is the anchor, or
adopt the GEE mode's explicit top/bottom vocabulary for both backends.

**Reproduction:** `harness/static_band_compare.py`, plus specs `X3_stac_18oct_only`,
`X4_stac_10nov_only`, `X5_stac_16oct_only`.

---

## FAIL-4 (MEDIUM) — `.parquet` AOIs are advertised but never work

`aoi_io.VECTOR_EXTENSIONS` contains `.parquet` and the README lists it, but
`resolve_aoi(verify_readable=True)` validates with `gpd.read_file(resolved, rows=1)`,
which cannot read GeoParquet:

```
ValueError: Could not read AOI .../okara_test_data_cane.parquet:
'...parquet' not recognized as being in a supported file format.
```

The same file opens fine with `gpd.read_parquet` (n=1, EPSG:4326, correct bounds), so
the file is valid and the reader is wrong. Even bypassing the check, all four downstream
consumers fail identically: `gee_client.py:104`, `static_classify.py:112`,
`postprocess.py:215`, `static_pipeline.py:104`.

Every other advertised format works — `.shp`, `.gpkg`, `.geojson`, `.kml`, `.fgb` and a
zipped shapefile all resolve with bounds matching the reference exactly.

**Reproduction:** `harness/unit_tests.py`.

---

## FAIL-5 (MEDIUM) — A resumed static run folder accumulates unrelated products

`2_static_run_2/` ends up holding both `18_Oct_2025_and_10_Nov_2025/` (written by
`A2_cane_2025`) and `16_Oct_2025/` (written by `C2_second_resume` resuming the same run
with auto date selection). The folder no longer identifies one result; which product a
later stage picks depends on what the current config's date suffix happens to be.
`run_info.json` does record both attempts, which is how this was traced.

---

## FAIL-6 (HIGH) — An 83%-cloud static image is classified anyway, with no gate

**What happens.** For `A1_cane_2016`, farmdar's cloud-aware selector reported:

```
static: dates=['2016-11-14'] (anchor=2016-11-14) -> 17.15% of AOI usable
```

Only 17% of the AOI is cloud-free on the chosen date. cropstack logs that line and
carries on. `_acquire_static_from_stac` fetches with `mask_clouds=False`, so the image
comes back complete but ~83% cloud, and `classify_static_image` runs XGBoost over cloud
tops as though they were ground.

**Why it is invisible.** The output looks healthy: the in-AOI static raster is fully
populated (`{1: 39,686, 4: 2,378,982}` — no gaps), the run reports 269 features /
768.5 acres, and nothing in the log says the number rests on cloud. Every 2025 run in
this campaign selected a date with "100.00% of AOI usable", so the contrast is available
and simply unused — nothing reads `result["selection"]["coverage_pct"]` beyond logging
it.

**Fix.** Gate on the selector's own `coverage_pct`: fail, or at minimum warn loudly,
below a configurable floor — the same way `build_crop_mask` already asserts on a
degenerate mask.

**Reproduction:** spec `A1_cane_2016`; see `logs/A1_cane_2016.log`.

---

## FAIL-7 (HIGH) — `static_worker_count` is derived from cores and ignores model size

**What happens.** `A1_spr_maize_2025` peaked at **27.89 GiB** of tree RSS — 4.9x the
cane and wheat runs — entirely inside `static_classify`:

| scenario | `static_classify` peak | mask px kept |
|---|---|---|
| `A1_wheat_2025` | 4.67 GiB | 1,407,169 |
| `A1_cane_2025` | 5.44 GiB | 153,043 |
| `A1_spr_maize_2025` | **27.89 GiB** | 439,255 |
| `A2_spr_maize_2025` | 25.57 GiB | 439,255 |

It is not the pixel count — wheat masks 3.2x more pixels for one sixth of the memory.
It is the model. `classify_static_image` sets `worker_count = cpu_count() - 1` (7 here)
and each spawn worker loads its own copy of the static model (deliberate: nothing large
is pickled per task). Measured, each in a fresh process:

| crop | model on disk | +RSS when loaded | worker total | trees |
|---|---|---|---|---|
| wheat | 0.7 MB | 11 MB | 210 MB | 167 |
| cane | 27.5 MB | 285 MB | 484 MB | 600 |
| spr_maize | 563.2 MB | **5,165 MB** | 5,365 MB | 291 |

The spr_maize static XGBoost expands **9.2x** from its 563 MB JSON to 5.2 GiB resident,
once per worker. 7 x 5.365 GiB = **37.6 GiB of models alone**; the observed 27.89 GiB is
consistent with ~5 of the 7 workers receiving one of this AOI's 4 windows.

**Why it gets worse, not better, at scale.** The pool is sized from cores, never from the
model, so the same config on a 32-core box would attempt 31 x 5.2 GiB = 161 GiB. And at
district scale every worker does get windows, making the model term a flat ~37.6 GiB
floor on top of the raster working set — with NDVI acquisition already wanting 11-46 GiB
(BOTTLENECKS §4), this is the most likely OOM in the codebase. Here it was also pure
waste: 7 workers spawned for 4 windows.

**Mitigation exists but is not the default.** `static_worker_count` is a real config
field (`config.py:243`); setting it to 2-3 for spr_maize caps the models at ~16 GiB.
Better: size the pool as `min(cpu_count()-1, n_windows, budget // model_rss)`, measuring
`model_rss` once in the parent after a single load.

**Reproduction:** `harness/static_model_memory_probe.py`; specs `A1_spr_maize_2025`,
`A2_spr_maize_2025`.

---

## FAIL-8 (HIGH) — The pre-2018 GEE/Landsat static path completes but returns an implausible result

Same AOI, same year, same resumed STAC NDVI stage, same crop; only the static backend
differs:

| scenario | static backend | features | acres |
|---|---|---|---|
| `A1_cane_2016` | STAC (Sentinel-2, 10 m) | 269 | 768.5 |
| `A3_cane_2016` | GEE (Landsat 8, 30 m) | 11 | **9.93** |

A 77x discrepancy, and it exits 0.

**The mechanics are all correct**, which is what makes it hard to spot: `gee_sensor_mode`
switches to LANDSAT below `gee_landsat_cutover_year`, the output really is 30 m
(668x542 vs the 10 m run's 2226x2226), the resolution-aware sieve threshold drops to
`static_sieve_min_pixels=1` exactly as the README promises, and Landsat 7 is correctly
never used. The crop mask was not the limiter either: 8,152 px at 30 m (~1,813 acres of
candidate ground), of which the model kept 9.93.

**The likely cause is the synthesised red-edge band.** `gee_client.homogenize_landsat8`
applies the right Collection-2 scale factors and maps SR_B5→B8 correctly, but Landsat 8
has no red-edge band, so it fabricates one:

```python
red_edge = scaled.select("B4").add(scaled.select("B8")).divide(2).rename("B5")
```

The static XGBoost was trained on a real Sentinel-2 rededge1 (B5, 705 nm), which for
vegetation sits far closer to NIR than to the midpoint of red and NIR. The docstring is
honest that the band is synthesised; what this run adds is the measured consequence.

**Recommendation.** Validate the Landsat static path against ground truth before using it
for pre-2018 reporting, or train a Landsat-specific static model. At minimum, log a
warning when a synthesised band is substituted for a trained one.

**Reproduction:** specs `A1_cane_2016` vs `A3_cane_2016`.

---

## FAIL-9 (MEDIUM) — `api_manual` accepts a date with no scene and fails opaquely minutes later

The only non-zero exit in the campaign:

```
RuntimeError: 1 GEE export task(s) failed.
(preceding log line) Task 5OAYS3PCYSYQPRNNIS5ED5SA FAILED: Image.select: Band
pattern 'B2' was applied to an Image with no bands.
```

Config: `year="2016", static_source="gee", gee_static_mode="api_manual",
gee_static_bottom_date="2016-10-15", gee_static_top_date="2016-11-14"`.

**Root cause.** Below `gee_landsat_cutover_year` the sensor silently becomes Landsat 8,
and Landsat 8 has no acquisition over this AOI on either date — those are *Sentinel-2*
dates. `build_static_composite`'s `one_day_mosaic` builds an empty collection,
`.mosaic()` yields a band-less image, and `.select(bands)` raises. GEE's own `api_auto`
picks the real Landsat dates (2016-10-17, 2016-11-02); re-running with those
(`A4b_cane_2016_landsat_dates`) completes, confirming the path itself is sound.

**Why it matters beyond one bad date choice:**
* Nothing validates the dates — `PipelineConfig.validate()` only checks that *some* were
  supplied, so a typo or sensor mismatch survives config time.
* The failure surfaces ~20 s later, after the export task has been submitted and
  scheduled, and the actionable message is in a separate ERROR log line, not in the
  exception the caller sees.
* The sensor switch is invisible where the user types the dates. Picking dates from
  Sentinel-2 availability is the natural instinct, since the NDVI half of the same run
  *is* Sentinel-2.

**Cheap fix.** In `build_static_composite`, check `collection.size()` for each requested
day before mosaicking and raise `no {sensor_mode} acquisition on {date} over this AOI`
client-side, before submitting an export.

**Reproduction:** spec `A4_cane_2016` (fails), `A4b_cane_2016_landsat_dates` (passes).

---

## OBS-6 (LOW) — Misleading error when both AOI spellings are passed

`build_pipeline_config` uses
`aoi_path = aoi_path or overrides.pop("aoi_shapefile", None)`. Python short-circuits, so
a truthy `aoi_path` leaves `aoi_shapefile` in `overrides`, and the unknown-field check
reports `TypeError: Unknown PipelineConfig field(s): ['aoi_shapefile']`. Rejecting the
ambiguous call is correct; calling the documented alias an unknown field is not.

---

## OBS-7 (LOW) — The degenerate-crop-mask guard measures against the wrong denominator

`build_crop_mask` aborts when `kept_pixels / (width * height) >= 0.90`, but the
denominator is the full static image grid — the AOI's **bounding box**, not the AOI. This
AOI's polygon covers only ~42% of its own bbox, so the guard cannot fire however
homogeneous the crop is; forcing `crop_classes=(1,4)` (every class present) still raised
nothing. Conversely, a rectangular AOI that genuinely is nearly all one crop would trip
the assertion on a perfectly legitimate result.

Same off-by-denominator makes the logged `Crop mask coverage: 3.09%` understate the crop
share of the AOI (153,043 px of the AOI's ~2.42 Mpx is ~6.3%). Observed headroom in the
real runs was 3.09% (STAC grid) and 4.71% (GEE grid) — nowhere near the threshold.

**Reproduction:** `harness/crop_mask_guard_probe.py`.

---

## Environment note (not a cropstack defect)

Every GDAL mosaic/warp floods the log with
`Warning 1: PROJ: proj_create_from_database: Open of /opt/gis/share/proj failed`.
Verified harmless here — outputs keep EPSG:4326, correct nodata and correct transforms.
It is this image's `osgeo` PROJ search path, not the repository.

---
---

# Retest against `c2f954d` — 2026-09-03

The findings above were raised against `b013476`. They were re-tested after the priority
window / sensor era / field-log changes; full detail in `RETEST_REPORT.md`.

**Closed:** FAIL-1 (sieve now removes exactly the −8,984 px the original probe predicted),
FAIL-2 (vector resume compares a source-raster fingerprint and rebuilds), FAIL-3 and
FAIL-6 (the 2016 run rejects the 16.9%-coverage scene and walks to a 98.3% one), FAIL-4 (a full fresh `.parquet` run reproduces the `.shp` result exactly: 800 features / 1,855.5 acres), FAIL-8 (Landsat static refused at `validate()`), FAIL-9
(bad GEE date caught client-side in 7 s), OBS-7 (mask coverage now reported against the
AOI, 6.34%, matching the independent measurement).

**Not closed:** FAIL-5 (run folders still accumulate unrelated date products) and FAIL-7
(peak 27.89 → 25.65 GiB, −8%; the pool is now bounded by model size, which closes the
32-core blow-up, but the design still permits half of free RAM in model copies).

The findings below are new in `c2f954d`.

---

## FAIL-10 (HIGH) — the AOI-hygiene fix was applied to the GEE path only

`gee_client.split_aoi_into_grid` now repairs invalid geometry, drops non-polygons and
raises clearly on an empty AOI. The STAC path — the default, and the backend the README
now recommends for NDVI — inherits none of it. Same AOIs through both:

| AOI | GEE grid split | STAC path (real `run_pipeline`) |
|---|---|---|
| valid polygon + self-intersecting bow-tie | repaired, 18 cells | `GEOSException: TopologyException: side location conflict at 73.428118…` |
| empty layer | `ValueError: AOI contains no features` | `ValueError: cannot convert float NaN to integer` |
| points only (no polygon) | `ValueError: AOI has no polygon geometry after cleaning` | validates, creates run folders, loads the model, **starts acquiring imagery** |

The last row is the serious one: an AOI with no polygon at all passes `cfg.validate()` and
proceeds to download Sentinel-2 tiles; it ran two minutes before being killed.
`aoi_io.resolve_aoi(verify_readable=True)` accepts all three without comment, so nothing
between the caller and farmdar inspects geometry on the STAC side.

**Fix:** move the cleaning block out of `split_aoi_into_grid` into `aoi_io.resolve_aoi` or
`PipelineConfig.validate`, so both backends inherit it.
**Repro:** `harness/retest_geometry_checks.py`; specs `N_empty_aoi`, `N_real_plus_bowtie`,
`N_points_only`.

---

## FAIL-11 (HIGH) — priority windows fix *which* date is chosen, not *whether* it is defensible

Holding AOI, year, NDVI stage and model fixed and varying only the static date **within
spr_maize's own three configured windows**:

| static date(s) | source | features | acres | % of AOI |
|---|---|---|---|---|
| 2025-05-14 | old auto pick | 315 | 931.7 | 1.8% |
| 2025-05-09 + 2025-05-01 | **new window 1 — what ships** | 1,070 | 3,992.8 | 7.8% |
| 2025-04-29 | window 2 | 1,945 | 8,322.0 | 16.2% |

**8.9× spread inside the crop's own preference list.** Window 1 scored 99.12%, window 2
scored 100.0%; the pipeline takes window 1 because it is first past the 80% floor and
never compares them. Coverage cannot discriminate — every candidate scores 99–100%.

Consequences: the reported spr_maize acreage moved 833.9 → 3,992.8 between commits
(re-running the old date under the new code gives 931.7, so +11.7% is the sieve fix and
the remaining **4.3× is purely the date**); and the old FAIL-3 date-sensitivity is not
removed but *concealed*, behind a log line reading
`Accepted window 1/3 at 99.1% coverage (floor 80%)` — which reads like a validated result.

This is not an argument against priority windows: the same mechanism rescues the 2016 case
outright (FAIL-6). It is an argument that coverage % is the wrong sole tie-breaker.
**Suggested fix:** prefer single-date over multi-date windows at comparable coverage, and
log when two windows are within a few points instead of silently taking the first.
Without ground truth none of 931.7 / 3,992.8 / 8,322.0 can be called correct.
**Repro:** specs `P4_sprmaize_2025`, `S1_sprmaize_0514_old`, `S2_sprmaize_0429_win2`,
`S3_sprmaize_0509_only`.

---

## FAIL-12 (MEDIUM) — static classification is not written atomically

`static_classify.py:401` opens the final output path directly. An interrupted static stage
leaves a **zero-byte `_Cls.tif`**, and the next resume opens it and dies:

```
rasterio.errors.RasterioIOError: '…/static_mosaic_29_Apr_2025_Cls.tif'
    not recognized as being in a supported file format.
```

The NDVI side already gets this right — inference workers write `…_predicted.tmp.tif` and
`.replace()` on completion (`inference_workers.py:164, 247`), which is why the previous
campaign's SIGKILL test resumed to a byte-identical product. The static stage never
adopted the pattern, so on a host that is culled without warning a routine interruption
becomes a run that cannot resume without manual cleanup.
**Fix:** write to `.tmp.tif` and rename, as the NDVI workers do.
*Trigger in evidence was a run killed by the tester, not by cropstack; the write path that
allows it is unconditional.*

---

## OBS-8 (LOW) — two log-message defects in the new selector

* In `auto` mode via priority windows the anchor warning is prefixed **"Manual static
  dates:"** — `Manual static dates: 2025-11-10 is the ANCHOR` on a run with
  `stac_static_mode="auto"`. It also fires for single-date selections, where there is no
  layering and no anchor to get wrong.
* The coverage floor is printed with `{floor:.0f}`, so `stac_static_min_coverage_pct=99.9`
  logs as `floor 100%` and `100.5` logs as `the 100% coverage floor` — during this retest
  that briefly made a passing run look like a failing one.
