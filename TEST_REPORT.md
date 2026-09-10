# TEST_REPORT.md — cropstack validation campaign

**Subject:** `cropstack` @ `b013476` (FAO unified crop-mapping pipeline)
**Date:** 2026-09-03 · **Duration:** 2.4 h of pipeline compute, 71.2 GiB fetched
**Box:** 8 vCPU (4 physical, HT) / 61 GiB / NVMe · Python 3.11.15 · GDAL 3.13.2 (`osgeo`
present, so mosaics take the streaming VRT path) · rasterio 1.4.3 · geopandas 1.0.1 ·
xgboost 3.2.0 · scikit-learn 1.6.1
**AOI:** `okara_test_data_*.shp`, 51,293 acres (all four crop AOIs are byte-identical,
md5 `883becff…`)

Companion documents: **`FAILURES.md`** (every defect, with reproduction) and
**`BOTTLENECKS.md`** (where time and memory actually go). Raw evidence is in
`metrics/` (per-scenario samples, events, validation) and `logs/`.

---

## 1. Verdict

**The pipeline runs, and it is reproducible — but it does not defend its own outputs.**

34 instrumented scenarios: **33 exited 0, one exited non-zero.** That headline is
misleading on its own, and it is the main thing this report has to say: of the eleven
defects found, **eight are silent**. The pipeline exits 0 and writes a well-formed,
plausible-looking GeoPackage that is wrong — built on an 83%-cloud image, or on a scene
covering 16% of the AOI, or copied from a previous run of a different date entirely.
Nothing in the exit code, the logs at INFO, or the output schema distinguishes those from
a good run.

Three findings are worth stating in one line each:

* **A resumed run can return a stale vector product from different raster data**
  (FAIL-2). `C2_second_resume` reports its own freshly-computed static raster in
  `sieved_static_raster` while `vector_output` points at a previous run's polygons: it
  returns 465.5 acres where that raster correctly vectorises to 2,120.3 — **4.6× low**,
  exit 0.
* **The static-stage sieve has never removed a pixel** (FAIL-1). It is called with
  `nodata_val` set to the background class, which masks out 97.7% of the raster, so the
  sieve has no valid neighbour to work with. The output is still named `…_sieved_p20.tif`.
* **A 563 MB model × 7 workers is a 37.6 GiB memory floor** (FAIL-7). The static worker
  pool is sized from CPU count and never from model size; spr_maize peaked at 27.9 GiB
  here and would need 161 GiB on a 32-core box.

What is genuinely good is the *core science path*: both imagery backends, all four crops,
every advertised AOI format bar one, and full reproducibility (§3). The defects are
almost entirely in the guard rails, the resume logic and the resource sizing — not in the
classification itself.

---

## 2. Coverage

| Dimension | Covered | Not covered |
|---|---|---|
| Crops | cane, wheat, spring maize, rice | — |
| Years | 2025 (modern S2), 2016 (pre-baseline / Landsat era) | — |
| NDVI backend | STAC, GEE | — |
| Static backend | STAC (auto + manual dates), GEE (`api_auto`, `api_manual`, `manual_gcs_link`) | — |
| AOI formats | `.shp`, `.gpkg`, `.geojson`, `.kml`, `.fgb`, zipped `.shp`, `.parquet` | `gs://` end-to-end (sandbox blocked the bucket write; behaviour verified directly against `pipeline.default_output_dir` instead, 4/4) |
| Run modes | `resume`, `new`, per-stage mixes, resume-after-SIGKILL (OK-7) | — |
| Tuning | `stac_tile_size_deg`, `stac_worker_count`, `ndvi_worker_count` | `static_worker_count` swept only by inference from FAIL-7 |
| Scale | one 418 km² AOI, extrapolated to district and province | no real district-scale run |

Full per-scenario table: `metrics/tables.md`.

---

## 3. What was proven correct

These are the results that let the rest of the report be trusted; each is a controlled
experiment, not an absence of errors.

**Reproducibility (OK-3).** `C3_new` re-acquired imagery, re-ran RF inference, re-mosaicked,
re-sieved, re-classified and re-vectorised from scratch — 8 minutes of fully independent
work — and reproduced `A1_cane_2025` exactly: same feature count, same acreage,
**byte-identical** sieved NDVI rasters. STAC acquisition, Whittaker smoothing, RF
inference and the sieve are all deterministic.

**Backend parity, static (OK/FAIL-3a).** Same date (2025-11-10), different backend:
STAC 1,817.9 acres / 795 features vs GEE 1,808.1 / 788 — **0.5% apart**.

**Backend parity, NDVI (OK-6).** `A3_cane_2025` (STAC NDVI) and `G_gee_ndvi_cane_2025`
(GEE NDVI) consume the *same* GEE static image, so the NDVI backend is the only variable:
153,043 vs 152,668 class-1 pixels in the AOI — **0.2% apart**, IoU 0.947, final acreage
within 0.95%.

Together these retire an earlier draft finding of mine (F-5) that claimed the two backends
delivered different radiometry. They do not. `farmdar.sentinel` handles the baseline-04.00
+1000 DN offset correctly and gates it on `s2:processing_baseline`; what I had actually
measured was `odc.stac.load()` bypassing farmdar entirely. **The two backends are
interchangeable for product and differ only in cost.**

**AOI format equivalence (OK-5).** `.shp`, `.gpkg` and `.geojson` of the same AOI give
1,309 features / 3,134.6391 acres / 245,245 class-1 pixels — identical to the last
decimal. `.kml`, `.fgb` and a zipped shapefile resolve with matching bounds.

**Multi-date layering (OK-4).** When the anchor date is cloud-free and covers the AOI, a
second date is correctly a no-op: wheat `A1` (1 date) and `A2` (2 dates) produce
byte-identical rasters, 0 differing pixels of 4,955,076. Same on the GEE side.

**Run-folder mechanics (OK-1).** Resume/new/tagged/explicit-id folder selection all behave
as documented, including Windows path repair (`\f`-mangling, `C:\…` → `/mnt/c/…`).

**Crash recovery (OK-7).** `C4a_kill_target` was SIGKILLed — parent and all three
children, no cleanup handlers — 26 s into RF inference, leaving four complete acquired
tiles (630 MB) and two *partial* `.tmp.tif` prediction files on disk. The resume finished
**status 0 in 83.6 s**: all four tiles were skipped as already acquired (50 MB of network
instead of 5,043 MB), inference re-ran from scratch rather than trusting the partials, and
both the NDVI and static sieved rasters came out **byte-identical** to the clean
`A1_cane_2025` baseline — 932 features / 2,120.35 acres.

This matters beyond the pass. Predictions are written to `.tmp.tif` and renamed only on
completion, so half-written work can never be mistaken for finished work, and the
checkpoint granularity is the acquired tile — the expensive unit. **The same codebase gets
exactly right, one stage earlier, what FAIL-2 gets wrong.** FAIL-2 is not a flaw in the
idea of resuming; it is one stage checkpointing on a filename instead of on content.

**Sieve memory (OK / BOTTLENECKS §5).** `postprocess.py`'s "roughly 8 bytes/pixel" claim
is accurate — measured 8.21 B/px across 5.0 → 242.8 Mpx in fresh processes. Sieve and
vectorise are comfortable to district scale and only strain at province scale.

---

## 4. Defects

Full detail and reproduction for each in **`FAILURES.md`**.

| # | Sev | Defect | Silent? |
|---|---|---|---|
| FAIL-1 | HIGH | Static-stage sieve is a no-op | yes |
| FAIL-2 | HIGH | `run_mode="resume"` returns a stale vector product from other raster data | yes |
| FAIL-3 | HIGH | A 16%-footprint static scene is accepted, and as anchor corrupts the other 84% | yes |
| FAIL-6 | HIGH | An 83%-cloud static image is classified with no gate | yes |
| FAIL-7 | HIGH | `static_worker_count` ignores model size — 27.9 GiB peak, OOM path at scale | yes |
| FAIL-8 | HIGH | Pre-2018 GEE/Landsat static path returns 9.93 acres where Sentinel-2 returns 768.5 | yes |
| FAIL-4 | MED | `.parquet` AOIs are documented and allow-listed but never work | no |
| FAIL-5 | MED | A resumed static run folder accumulates unrelated products | yes |
| FAIL-9 | MED | `api_manual` accepts a date with no scene, then fails opaquely minutes later | no |
| OBS-6 | LOW | Misleading error when both AOI spellings are passed | no |
| OBS-7 | LOW | Degenerate-crop-mask guard measures against the wrong denominator | yes |

**The pattern worth acting on.** FAIL-3, FAIL-6 and FAIL-9 are the same defect wearing
three coats: *the metadata needed to reject a bad input is already in hand and simply
never checked.* The cloud-coverage percentage is logged and discarded; the scene footprint
is in the STAC item; the empty Landsat collection is one `.size()` call away. Each is a
few lines of validation, and each currently costs a silently wrong crop map.

**Result sensitivity, for context.** On the same AOI and year, with the NDVI stage held
bit-identical, the reported cane acreage ranges from **365.6 to 2,120.3 acres — 5.8×** —
depending only on which static date is chosen and how the dates are ordered:

| static configuration | acres |
|---|---|
| 2025-10-18 alone (the 16%-footprint scene) | 365.6 |
| [2025-10-18, 2025-11-10] — 10-18 as anchor | 465.5 |
| 2025-11-10 alone | 1,817.9 |
| 2025-10-16 alone (what `auto` picks) | 2,120.3 |

The headline number is not robust to configuration, and nothing in the output
communicates that.

---

## 5. Performance

Full analysis in **`BOTTLENECKS.md`**. In brief:

* **Acquisition is 89% of wall clock** (487 s of a 546 s baseline run). Everything the
  repo optimised — streaming mosaic, 8 B/px sieve, worker recycling, dissolve removal —
  totals about **8 seconds**. Tuning compute cannot help; fetching less data can.
* **The CPU is idle almost everywhere.** Peak utilisation is during RF inference; the
  rest is network or GEE-queue latency. Adding cores would not help.
* **Two measured levers:** `stac_tile_size_deg=0.2` buys 22% off both wall clock and
  network for 19% more RAM; `stac_worker_count=2` buys 10% peak RSS for 14% more wall
  clock. Both produce identical products. The larger prize is the unconfigured
  read-through cache (`FARMDAR_S2_CACHE_BUCKET`), which matters *across* runs — every
  scenario in this campaign re-fetched the same imagery.
* **GEE trades wall clock for network on both stages.** GEE NDVI is 2.9× slower but uses
  4.3× less network and 1.75 GiB less RAM, spending 99.8% of the stage below 30% CPU
  waiting on the export queue. Submitting exports before the NDVI stage would recover
  most of it.
* **Pre-2018 STAC years cost ~18× the network** for a worse product: 39.8 GB and 953 s
  versus 2.3 GB and 401 s for the same AOI in 2025.
* **Memory has a second, larger story:** see FAIL-7 — the biggest peak in the campaign is
  a model-loading problem, not an imagery problem.

---

## 6. Recommendations, in priority order

1. **Fix the sieve's `nodata_val`** (FAIL-1) — a one-line change that currently makes a
   named output stage a placebo.
2. **Make resume identity-aware** (FAIL-2, FAIL-5). Key the vector short-circuit on the
   source raster's identity (path + mtime, or a content hash recorded in `run_info.json`),
   not on whether a filename exists. This is the defect most likely to put a wrong number
   in a report.
3. **Validate inputs where the metadata already exists** (FAIL-3, FAIL-6, FAIL-9): gate on
   the selector's `coverage_pct`; reject or warn on a static scene whose footprint covers
   a small fraction of the AOI; check `collection.size()` before submitting a GEE export.
4. **Size the static worker pool from memory, not cores** (FAIL-7):
   `min(cpu_count()-1, n_windows, budget // model_rss)`.
5. **Document that `stac_static_dates[0]` is the anchor** (FAIL-3), or adopt the GEE
   mode's explicit top/bottom vocabulary on both paths. Writing dates chronologically —
   the natural instinct — is what selects the older, partial scene as the radiometric
   reference.
6. **Either fix `.parquet` or remove it from the docs and the allow-list** (FAIL-4).
7. **Validate the Landsat static path against ground truth, or train a Landsat-specific
   model** (FAIL-8), and warn in the log whenever a synthesised band is substituted for a
   trained one.

---

## 7. Limitations of this campaign

Stated so the results are not over-read:

* **One AOI, 418 km².** District and province figures in `BOTTLENECKS.md` are
  extrapolations from measured per-pixel and per-tile costs, clearly labelled as such. No
  district-scale run was executed.
* **No ground truth.** Every accuracy statement here is *relative* — backend against
  backend, run against run, date against date. Nothing in this campaign establishes that
  any of these acreages is correct, only which configurations disagree and by how much.
  FAIL-8's "implausible" is a 77× disagreement with the Sentinel-2 path, not a measured
  error against reality.
* **Network is sampled system-wide** (`psutil.net_io_counters()`) on a shared node. RSS
  and disk IO are process-tree-isolated; network is not. The large figures are sustained
  exactly across their stages, so attribution is near-certain but not proven.
* **`gs://` AOIs were not run end to end** — the sandbox blocked both writing a test AOI
  to the shared bucket and enumerating it. The specific output-path behaviour was verified
  directly against `pipeline.default_output_dir` instead (4/4 pass).
* **Two of my own earlier findings were retracted after better measurement** (a claimed
  radiometry mismatch, and a claimed 4× duplicate download that measured 22%). Both
  retractions are documented in place rather than deleted, and both came from measuring a
  reimplementation instead of the pipeline's own inputs. Where this report states a
  number, it is from the pipeline's own outputs.
