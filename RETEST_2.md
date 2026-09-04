# RETEST_2.md — cropstack @ `ce37be8`

Second re-validation. Scope set by the field log: the two open defects (**FAIL-11**
window comparison, **FAIL-12** atomic writes) and the retrospective fixes shipped with
them — empty results, geometry type, result check, window expansion, tile throughput —
plus a regression control.

**Subject:** `ce37be8` "Compare every acquisition window, write rasters atomically, check
the result" (was `c2f954d`) · **Date:** 2026-09-04
**Same AOIs, same `runs_retest/` directory, same harness as `RETEST_REPORT.md`,** so every
number below is directly comparable to the one it replaces.

---

## Lead

| # | Sev | Item | State |
|---|---|---|---|
| **FAIL-11** | HIGH | Window comparison — all windows scored, spread logged, both levers work | **FIXED** (see caveat R2-3) |
| **FAIL-12** | MED | Atomic writes — classify, mosaic and sieve all survive SIGKILL | **FIXED** |
| — | — | Empty results — GPKG+ZIP with schema and 0 rows, path returned, batch survives | **FIXED** |
| — | — | Geometry type — every new layer is MultiPolygon; two-district append works | **FIXED** |
| — | — | Result check — `result_check.json` present with all four fields | **FIXED** |
| — | — | Window expansion — 5 d/side ×3, each logged; `=0` fails cleanly | **FIXED** |
| — | — | Regression — pinned-date `.shp` case | **0.0% delta** |
| **R2-1** | **HIGH** | A transient STAC error silently drops a window from the comparison; the run proceeds on a different window, 2.1× the acreage, exit 0 | **NEW** |
| **R2-2** | **HIGH** | Static staging tiles are reused across dates — a resumed run classifies the *previous* attempt's imagery and files it under the new date's name | **NEW** |
| **R2-3** | **MED** | The NDVI stage does not notice a year with no imagery; with the new empty-output path it now delivers a clean "0 acres" product instead of failing | **NEW** |
| **R2-4** | **MED** | `qc_*_static_retention_pct` bounds are far too wide for real data, and the floor false-positives on genuinely crop-free ground | **NEW** |
| **R2-5** | LOW | `stac_slow_tile_warning_minutes` is applied to a wall/tiles ratio, so it is scaled down by worker count and cannot fire in practice | **NEW** |

Two HIGH findings are **new and both were surfaced by this retest's own runs, not
constructed.** R2-1 happened spontaneously during the FAIL-12 kill test; R2-2 was found
while explaining R2-1's aftermath and then reproduced deterministically.

---

## 1. FAIL-11 — window comparison

### 1.1 The log now shows the whole comparison

`T1_sprmaize_margin5` (spr_maize 2025, okara, `stac_static_mode="auto"`, defaults):

```
INFO static_pipeline: Window scores (best-first by preference):
INFO static_pipeline:   window 1 (2025-05-01 to 2025-05-10): ['2025-05-09', '2025-05-01'] -> 99.1% of AOI usable
INFO static_pipeline:   window 2 (2025-04-20 to 2025-04-30): ['2025-04-29'] -> 100.0% of AOI usable
INFO static_pipeline:   window 3 (2025-05-11 to 2025-05-20): ['2025-05-14'] -> 100.0% of AOI usable
INFO static_pipeline: Coverage spread across windows: 99.1%-100.0%. The chosen date, not just the code, determines the acreage reported.
INFO static_pipeline: Preferring window 1 (2025-05-01 to 2025-05-10) at 99.1% over the highest-coverage 100.0%: within the 5-point margin, and it is the agronomically better date.
```

Every window is scored; the spread line is printed; the choice states its reason. This is
what FAIL-11 asked for. **Which window and why:** window 1 (2025-05-09 + 2025-05-01), at
99.1% coverage — not because it scored best, but because it is the earliest window whose
coverage is within `static_window_preference_margin_pct` (5.0 points) of the best score
(100.0%), and earlier windows are the agronomically better dates.

### 1.2 The margin lever works, and the date-sensitivity is visible

All four runs hold AOI, year, model and NDVI stage fixed — the NDVI product is
bit-identical (`md5 a46463f7…`) across all of them — and vary only the static date.

| run | setting | window chosen | static date(s) | coverage | features | **acres** | % of AOI | retention |
|---|---|---|---|---|---|---|---|---|
| `T1_sprmaize_margin5` | margin **5** (default) | 1 | 2025-05-09 + 2025-05-01 | 99.12% | 1,070 | **3,992.79** | 7.8% | 20.5% |
| `T2_sprmaize_margin0` | margin **0** | **2** | 2025-04-29 | 100.0% | 1,945 | **8,322.02** | 16.2% | 42.4% |
| `T3_sprmaize_margin100` | margin **100** | 1 | 2025-05-09 + 2025-05-01 | 99.12% | 1,070 | **3,992.79** | 7.8% | 20.5% |
| `T4_sprmaize_startat3` | `static_window_start_at=3` | 3 | 2025-05-14 | 100.0% | 315 | **931.68** | 1.8% | **4.8%** ⚠ |

* **margin = 0 switches to the highest-coverage window** — window 2, 2025-04-29, **8,322.02
  acres**. Confirmed.
* **margin = 100 takes window 1 regardless** — **3,992.79 acres**. Confirmed.
* All three figures reproduce the FAIL-11 table to the pixel (931.7 / 3,992.8 / 8,322.0).
  **The 8.9× spread is unchanged. It is now printed, not hidden — but it is not reduced.**

Two things the fix genuinely buys beyond visibility:

1. `T4`, the window-3 date the *old* code used to pick, is now flagged by the result
   check: `The static model kept only 4.8% of the NDVI stage's crop area.` The lowest of
   the three candidates is the one that trips a warning. That is a real improvement — but
   see R2-4, it clears the 5.0% floor by 0.2 points.
2. `static_window_start_at` gives an operator a documented way to re-run a district.

**Caveat on the margin lever (R2-3, MED):** the margin can only reach the *first*
best-scoring window. Windows 2 and 3 both score 100.0%; `next(... >= best - margin)` takes
window 2 and window 3 is unreachable at any margin value. The 931.7-acre candidate can
only be selected with `static_window_start_at=3`. If two windows tie, later ones are
invisible to the lever.

**Repro:** `specs_retest2/T{1,2,3,4}_*.json`; `harness/retest2_window_probe.py margin`
(scores every window in ~3 s without downloading imagery);
`metrics/retest2_window_probe_margin.json`.

---

## 2. FAIL-12 — atomic writes

### 2.1 Static classification, killed mid-progress-bar

`harness/retest2_kill.sh` launches the run in its own process group, waits for the
`Classifying static image: 25%` marker, waits 3 s more, then `kill -9` on the whole group
— parent, `tee`, resource tracker and all four spawned XGBoost workers. Real SIGKILL, no
cleanup handlers.

State immediately after the kill:

```
      1516  runs_retest/K1_kill_static/2_static_run_1/29_Apr_2025/static_mosaic_29_Apr_2025_Cls.tif.tmp.tif
    116568  runs_retest/K1_kill_static/2_static_run_1/29_Apr_2025/static_mosaic_29_Apr_2025_Cls_crop_mask.tif
```

**No `_Cls.tif` at the final path.** Only the 1,516-byte `.tmp.tif`. The resume then ran to
completion, exit 0, no `RasterioIOError`. Confirmed.

### 2.2 Before/after control on the same inputs

To show the fix is what changed the outcome, `harness/retest2_prefix_control.py` pulls
`static_classify.py` out of git at `c2f954d` and puts it ahead of the repo on `sys.path`,
so the child imports the **old** classifier and the new everything else, against identical
imagery, mask and model, killed at the same marker:

| code | file at the final path | a resume opening it |
|---|---|---|
| `c2f954d` (pre-fix) | `static_mosaic_CTL_Cls.tif`, **0 bytes** | `RasterioIOError: … not recognized as being in a supported file format.` |
| `ce37be8` (post-fix) | *none* — only `…_Cls.tif.tmp.tif`, 0 bytes | n/a, recomputes |

That is the exact error `RETEST_REPORT.md` recorded for FAIL-12, reproduced on demand and
then shown gone.

### 2.3 Mosaic and sieve

`harness/retest2_kill_writer.py` runs each real function in a child process against real
run data and SIGKILLs it the moment it announces its write:

| write path | killed mid-write | final path after kill | verdict |
|---|---|---|---|
| `raster_io.mosaic_geotiffs` (48 inputs) | yes, `exit -9` | **absent**; `probe_mosaic.tif.tmp.tif` (27.9 MB) + `.tmp.mosaic.vrt` | **PASS** |
| `postprocess.apply_strict_directional_sieve` (44.6 Mpx input) | yes, `exit -9` | **absent**; `…_sieved_p20.tif.tmp.tif` | **PASS** |

All three write paths are atomic. FAIL-12 is fixed.
*Minor:* the mosaic leaves a `.tmp.mosaic.vrt` beside the `.tmp.tif`; the `except
BaseException` cleanup cannot run under SIGKILL, so both are litter, not corruption.
Nothing reads them.

**Repro:** `harness/retest2_kill.sh`, `harness/retest2_kill_writer.py`,
`harness/retest2_prefix_control.py`; `metrics/retest2_kill_writers.json`,
`metrics/retest2_prefix_control.json`; `logs/K1_kill_report.txt`.

---

## 3. R2-1 (HIGH, NEW) — a transient STAC error silently drops a window

This was not constructed. It happened during the `K1_kill_static` run above:

```
WARNING static_pipeline: window 1 (2025-05-01 to 2025-05-10): could not be scored
    (You have exceeded a rate limit. Contact planetarycomputer@microsoft.com.).
INFO static_pipeline: Window scores (best-first by preference):
INFO static_pipeline:   window 2 (2025-04-20 to 2025-04-30): ['2025-04-29'] -> 100.0% of AOI usable
INFO static_pipeline:   window 3 (2025-05-11 to 2025-05-20): ['2025-05-14'] -> 100.0% of AOI usable
INFO static_pipeline: Chose window 2 (2025-04-20 to 2025-04-30) at 100.0% (floor 80%).
```

A Planetary Computer rate limit removed the **preferred** window from the comparison.
`_score_window` catches the exception, logs a warning, returns `None`, and the window is
simply absent from `scored`. The run then proceeded on window 2 — **8,322.02 acres instead
of 3,992.79, a 2.1× difference** — and exited 0.

Why this matters more than an ordinary transient:

* The score table is titled *"Window scores (best-first by preference)"* and lists two
  rows. **Nothing in it says a third window exists and was never evaluated.** The
  `Chose window 2 … at 100.0% (floor 80%)` line reads like a validated comparison.
* `window_scores`, persisted into `run_info.json` and the run outcome, records the same
  two rows. The run's own audit trail is silently incomplete.
* Both surviving windows scored 100.0%, so the coverage-spread line — the one signal
  designed to say "the date is doing the work here" — was suppressed by
  `if spread and (max - min) > 0`.
* There is **no retry**. One HTTP 429 permanently decides the district's acreage.
* The new design *increases* exposure: scoring every window makes 3–4 catalogue calls per
  run where the old first-past-the-post code made 1–2, against the same rate limit.

**Fix:** distinguish "scored, unusable" from "could not be scored". A window lost to an
exception should be retried with backoff, and if it still fails, the run should either
refuse to choose (the preferred window is unknown, not rejected) or carry an explicit
`unscored: [window 1]` row into the score table, the log line and `window_scores`. As it
stands an infrastructure hiccup rewrites the answer by 2.1× with no trace beyond one
WARNING line 6 lines above a confident-looking conclusion.

**Repro:** `logs/K1_kill_static_killed.log` lines 37–45. Not deterministic (depends on the
rate limiter), but the code path is unconditional: any exception from `select_static_dates`
does this.

---

## 4. R2-2 (HIGH, NEW) — static staging tiles are reused across dates

Explaining R2-1's aftermath turned up something worse. The `K1_kill_static` **resume**
completed cleanly, selected window 1, and wrote:

```
runs_retest/K1_kill_static/2_static_run_1/09_May_2025_and_01_May_2025/
    static_mosaic_09_May_2025_and_01_May_2025_Cls_sieved_p20.tif
```

reporting `1,945 features, 8,322.02 acres`. But **window 1 produces 3,992.79 acres**
(`T1`, `T3`). 8,322.02 is the *29 April* answer. Checking the rasters:

| file | crop pixels | md5 |
|---|---|---|
| `T1` window 1 (`09_May…_sieved`) | 191,028 | `c7562afa5232` |
| `T2` window 2 (`29_Apr…_sieved`) | 395,796 | `5b2b3bc81b1f` |
| **`K1` resume (`09_May…_sieved`)** | **395,796** | **`5b2b3bc81b1f`** |

The resume's "9 May + 1 May" product is **byte-identical to the 29 April product.** The
pipeline's own log says why:

```
farmdar.sentinel: AOI -> 4 tiles @ 10m (…, dates=['2025-05-09', '2025-05-01'], 8 workers)
farmdar.sentinel: [1/4] tile 0002: skipped_exists
farmdar.sentinel: [2/4] tile 0001: skipped_exists
farmdar.sentinel: [3/4] tile 0003: skipped_exists
farmdar.sentinel: [4/4] tile 0004: skipped_exists
farmdar.sentinel: DONE 4 tiles in 0.0 min. dates=['2025-05-09', '2025-05-01']
```

`static_pipeline.run_static_pipeline` clears `static_staging/` only on the **success** path
(`if cfg.delete_raw_static_tiles: shutil.rmtree(...)`, line 400). The killed run had already
downloaded 29 April into it. Staging tiles are named `static_10m_tile_0001.tif` — **the
filename carries the tile index and resolution but not the date** — so the next run's
request for 9 May matched them and skipped all four downloads.

The result is 29 April pixels written under a 9 May filename, in a 9 May folder, with a
`run_info.json`, a returned `sieved_static_raster` and a vector `source.json` that all say
9 May. **Every audit trail in the run is internally consistent and wrong.** Exit 0.

The vector stage's source-record guard (the FAIL-2 fix) cannot catch it: it compares path,
size and mtime of the raster it was handed, and that raster genuinely was rewritten.

### Deterministic reproduction, no kill and no rate limit required

`K4_staging_reuse`, two runs in one output folder with `delete_raw_static_tiles=false` — a
documented, supported setting:

| step | requested dates | acquisition | product | crop px | md5 |
|---|---|---|---|---|---|
| `K4a` | `["2025-04-29"]` | 4 tiles downloaded | `29_Apr_2025_Cls_sieved_p20.tif` | 395,796 | `5b2b3bc81b1f` |
| `K4b` | `["2025-05-09"]` | **4 × `skipped_exists`, 0.0 min** | `09_May_2025_Cls_sieved_p20.tif` | **395,796** | **`5b2b3bc81b1f`** |

`K4b` reports **8,322.02 acres for 2025-05-09**. `S3_sprmaize_0509_only` in the previous
campaign measured 2025-05-09 at 3,992.8 acres.

**So the trigger is not only an interruption.** Any static run that starts with a populated
`static_staging/` classifies whatever is in it:
* a run killed / culled between acquisition and cleanup (i.e. exactly FAIL-12's scenario),
* any run with `delete_raw_static_tiles=False`,
* any resume whose selected dates changed — which R2-1 shows can happen from a rate limit
  alone.

**Interaction with the FAIL-12 fix.** Before `ce37be8`, this resume path crashed loudly on
the 0-byte `_Cls.tif` before it could mis-classify. The atomic-write fix is correct in
itself, but by removing that crash it **converted a loud failure into a silent wrong
answer**. The two changes need to land together.

**Fix:** put the date set into the staging path (`static_staging/<date_suffix>/`) or into
the tile filename, and clear `static_staging/` at the *start* of an acquisition whose dates
differ from `static_selection.json`. A `try/finally` around the static stage that removes
staging on any exit would also close the interruption case.

**Repro:** `specs_retest2/K4a_staging_0429_keep.json`, `K4b_staging_0509_reuse.json`;
`logs/K4b_staging_0509_reuse.log` lines 37–43.

---

## 5. Empty results

`Z1_nocane_empty` — a **2.25 × 2.25 km clipped sub-AOI (1,324.7 acres) inside the Okara
cane AOI, chosen because the pipeline's own cane classification returns zero class-1
pixels over it** (block at row 500 / col 1875 of `okara_test_data_cane_…_sieved_p20.tif`
is 100% class 4). Full pipeline, fresh imagery, `stac_static_mode="auto"`.

```
  [Warning] No pixels found for classes [1].
  -> Writing an empty layer with the full schema (no pixels of classes [1]).
   -> Saved GPKG: …/Z1_nocane_empty/3_vector_run_1/final_output/okara_cane_2025.gpkg
   -> Saved ZIP (Shapefile): …/okara_cane_2025.zip
INFO pipeline: Pipeline finished in 3.2 min -> …/okara_cane_2025.gpkg
```

| check | result |
|---|---|
| GPKG written | yes, 98,304 bytes |
| ZIP written | yes, 807 bytes |
| columns | `predicted` (int64), `area_acres` (float64), `geometry` |
| rows | **0** |
| CRS | EPSG:4326 |
| pipeline return | a **path**, not `None`; exit 0 |

**Batch.** `jobs_batch_retest2.json` puts the empty job between two normal ones and runs
with **`--stop-on-error`**, so an exception would halt the batch:

```
Batch finished: 3 succeeded, 0 failed.
BATCH EXIT=0
```

| crop | district | status | vector_output |
|---|---|---|---|
| cane | okara | ok | `…/cane_normal/…/okara_cane_2025.gpkg` |
| cane | **okara_nocane** | **ok** | `…/cane_EMPTY/…/okara_nocane_cane_2025.gpkg` |
| wheat | okara | ok | `…/wheat_after_empty/…/okara_wheat_2025.gpkg` |

Confirmed on every point. **Caveat: see R2-3** — the same path also makes a completely
data-less run look like a valid zero.

---

## 6. Geometry type

| layer | rows | declared | actual types |
|---|---|---|---|
| `Z3_cane_auto` | 800 | MultiPolygon | `['MultiPolygon']` |
| `Z4_wheat_punjab` | 2,114 | MultiPolygon | `['MultiPolygon']` |
| `T1_sprmaize_margin5` | 1,070 | MultiPolygon | `['MultiPolygon']` |
| `Z1_nocane_empty` | 0 | *Unknown* | — (empty) |

Append, writing A then appending B into the **same layer**:

| case | result |
|---|---|
| cane (800) + wheat (2,114) | **ok, 2,914 rows, all MultiPolygon**, no warning |
| empty then real | ok, 800 rows, MultiPolygon |
| real then empty | ok, 800 rows, MultiPolygon |

Confirmed: only MultiPolygon, and two districts append into one layer.

**Two honest qualifications.**

1. **The pre-fix failure did not reproduce on this box as a hard error.** Campaign-1
   outputs (`runs/…`, `b013476`) are uniformly **`Polygon`**, not mixed — `explode()`
   guaranteed singlepart — and appending two of them succeeds (269 + 184 → 453 rows).
2. Mixing the two types on this GDAL build is a **warning, not a rejection**:
   `RuntimeWarning: A geometry of type MULTIPOLYGON is inserted into layer crop of
   geometry type POLYGON, which is not normally allowed by the GeoPackage specification,
   but the driver will however do it.` Appending an old (`Polygon`) product into a new
   (`MultiPolygon`) one gives 1,732 rows of **mixed type in a non-conformant GeoPackage**
   — silently. Stricter clients reject it.

So the fix is right and worth having: new outputs are uniform, and the warning disappears.
But the value it delivers here is *conformance*, not the recovery of a crash — and a mixed
estate of pre- and post-`ce37be8` products still produces non-conformant appends.

**Repro:** `harness/retest2_geometry_append.py`; `metrics/retest2_geometry_append.json`,
`retest2_append_order.json`, `retest2_append_crossversion.json`.

---

## 7. Result check

`result_check.json` is present in the vector run folder of **every** run, with all four
requested fields.

| run | static date(s) | `aoi_acres` | `crop_acres` | `crop_share_of_aoi_pct` | `static_retention_pct` | warning |
|---|---|---|---|---|---|---|
| **cane** `Z3_cane_auto` | 2025-11-10 (win 1) | 51,293.1 | 1,855.5 | 3.6 | **36.9** | none |
| **wheat/punjab** `Z4` | 2025-02-18 (win 1) | 51,293.1 | 18,404.3 | 35.9 | **30.2** | none |
| **wheat/sindh** `Z5` | 2025-02-03 (win 1) | 51,293.1 | 15,750.8 | 30.7 | **25.9** | none |
| **spr_maize** `T1` | 2025-05-09+05-01 (win 1) | 51,293.1 | 3,992.8 | 7.8 | **20.5** | none |
| *(context)* cane `Z2` pinned | 2025-10-16 | 51,293.1 | 2,184.7 | 4.3 | 43.5 | none |
| *(context)* spr_maize `T2` | 2025-04-29 (win 2) | 51,293.1 | 8,322.0 | 16.2 | 42.4 | none |
| *(context)* spr_maize `T4` | 2025-05-14 (win 3) | 51,293.1 | 931.7 | 1.8 | **4.8** | **fired** |
| *(context)* `Z1` empty | 2025-11-10 | 1,324.7 | 0.0 | 0.0 | **0.0** | **fired** |

**Did any warning fire?** Two, both on the retention floor: `T4` at 4.8% and `Z1` at 0.0%.
None of the four headline runs warned.

**Do the retention figures look sane?** Yes for the crops themselves. A static model that
keeps a quarter to a half of the NDVI stage's crop mask is what these Okara products have
always done, and the ordering is sensible: the two wheat regionalisations sit close (30.2%
vs 25.9%) on an identical NDVI input, and cane's two dates bracket them (36.9%, 43.5%).
Crop shares are plausible for an Okara Rabi/Kharif split — wheat at ~36% of the AOI, cane
at 3.6%, spring maize at 7.8%.

### R2-4 (MED, NEW) — the retention bounds are set wrong for real data

Observed healthy band across 4 crops, 2 regions and 6 date choices: **20.5% – 43.5%.**
Configured: `qc_min_static_retention_pct=5.0`, `qc_max_static_retention_pct=99.5`.

* The floor is **4.1× below** the lowest healthy value. Cane at 36.9% delivers 1,855.5
  acres; a static stage collapsing to 6% retention would still pass silently while
  reporting `1855.5 × 6/36.9 ≈ **302 acres — an 84% under-report**`.
* The ceiling is **2.3× above** the highest healthy value. Nothing between 43.5% and 99.5%
  is ever flagged, so a model keeping 90% of its input mask — contributing essentially
  nothing, which is the failure the docstring names — passes.
* The floor's one catch, `T4` at **4.8%**, cleared by **0.2 percentage points**. Set the
  floor at 4.5 and the campaign's known-bad date goes unflagged.

From this data I would set **floor ≈ 15%, ceiling ≈ 60%** — `T4` would then be caught with
10 points of headroom, and a do-nothing model caught long before 99.5%. Six observations on
one AOI is not enough to fix these permanently; they should be set per crop from a wider
sample. But 5/99.5 is not a bound, it is a formality.

* **False positive on genuine absence.** `Z1` and the batch's empty job are crop-free
  ground; retention is 0% because the honest answer is zero. The warning fires and asserts
  a cause that is wrong — *"That is the signature of a hazy or cloud-contaminated image
  rather than of a real crop boundary — re-run from the next window"* — and prescribes a
  re-run that would waste the operator's time. The module's own docstring says "a
  genuinely low-cropped district is a valid answer"; the retention warning does not honour
  that. Gate it on `ndvi_crop_pixels` being materially above zero, or soften the wording to
  state the observation rather than the diagnosis.

---

## 8. Window expansion

Forced with **spr_maize 2027** — a year with no Sentinel-2 imagery at all, and past the
`sentinel2_start_year` guard, so the static stage is genuinely reached. (Pre-2016 years
cannot test this: `config` downgrades them to NDVI-only before the selector runs.)

`Z6_noimagery_2027`, defaults (`expansion_days=5`, `max_expansions=3`):

```
WARNING window 1 (2027-05-01 to 2027-05-10): could not be scored (No Sentinel-2 scenes …).
WARNING window 2 (2027-04-20 to 2027-04-30): could not be scored (…).
WARNING window 3 (2027-05-11 to 2027-05-20): could not be scored (…).
WARNING No configured window had usable imagery; widening the leading window to 2027-04-26..2027-05-15.
        This date is outside the phenology the windows encode.
WARNING expanded window +5d (2027-04-26 to 2027-05-15): could not be scored (…).
WARNING No configured window had usable imagery; widening the leading window to 2027-04-21..2027-05-20.
        This date is outside the phenology the windows encode.
WARNING expanded window +10d (2027-04-21 to 2027-05-20): could not be scored (…).
WARNING No configured window had usable imagery; widening the leading window to 2027-04-16..2027-05-25.
        This date is outside the phenology the windows encode.
WARNING expanded window +15d (2027-04-16 to 2027-05-25): could not be scored (…).
RuntimeError: No usable static imagery in any configured window for spr_maize 2027
              (no window produced a usable acquisition).
```

**Exactly three expansions, 5 days each side (+5/+10/+15 from the leading window
2027-05-01..05-10), each logged as a departure from the crop's phenology.** Confirmed.

| variant | expansions attempted | outcome |
|---|---|---|
| `Z6` default | 3 (+5, +10, +15) | `RuntimeError`, exit 1 |
| `Z7_noimagery_2027_exp0` (`static_window_expansion_days=0`) | **0** | same `RuntimeError`, exit 1 |
| probe `max_expansions=1` | 1 (+5) | `dates=None` |

`=0` fails cleanly: no expansion attempted, one clear `RuntimeError` naming crop and year,
no traceback from inside GDAL. Confirmed.

**Repro:** `specs_retest2/Z6…`, `Z7…`; `harness/retest2_window_probe.py expansion`;
`metrics/retest2_window_probe_expansion.json`.

### R2-3 (MED, NEW) — the NDVI stage does not notice a year with no imagery

`Z6`'s **NDVI stage reported success on the same dataless year:**

```
INFO ndvi_pipeline: STAC acquisition took 0.3 min for 4 tile(s) (0.1 min/tile).
INFO pipeline: NDVI stage finished in 0.3 min -> …_rf_classification_map_sieved_p20.tif
```

The product it wrote is **100% nodata**: `{255: 4955076}` over all 4,955,076 pixels. The
tile-completeness check counts tiles produced (4/4), not whether any of them carry data.
Only the static stage caught the year.

On an NDVI-only crop nothing catches it. `Z9_rice2027_ndvionly` (rice has no static model,
so `run_static_model` is off):

```
  [Warning] No pixels found for classes [1].
  -> Writing an empty layer with the full schema (no pixels of classes [1]).
INFO qc: Result: aoi_acres=51293.1, feature_count=0, crop_acres=0.0, crop_share_of_aoi_pct=0.0
INFO pipeline: Pipeline finished in 0.5 min -> …/okara_rice_2027.gpkg
```

**Exit 0. Zero warnings.** A year with no satellite imagery whatsoever produces a clean,
schema-correct, zero-acre deliverable indistinguishable from a district where rice simply
is not grown. `qc_min_crop_share_pct` is `None` by default, and retention cannot be
computed without a static raster, so nothing fires.

This is an *interaction*, not a regression in the empty-output change itself: writing empty
outputs is right and was asked for, but it removes the last signal that used to distinguish
"no crop" from "no data". **Fix:** have the NDVI stage assert that its acquisition returned
some valid pixels — `farmdar.sentinel` already reports a `% filled` figure on the static
path — and fail, or at minimum warn loudly, when the classification map is entirely nodata.

---

## 9. Tile throughput

Every fresh STAC acquisition in this retest:

| run | AOI | tiles | wall | **reported min/tile** | warning |
|---|---|---|---|---|---|
| `Z8_cane_fresh_ndvi` | full Okara, full cane series | 4 | 3.0 min | **0.8** | no |
| `Z1_nocane_empty` | 2.25 km block, full cane series | 1 | 1.9 min | **1.9** | no |
| `Z6` / `Z9` (2027, no data) | full Okara | 4 | 0.3–0.4 min | 0.1 | no |

**No run fired the warning, and none of them should have.** `Z8` fetching a full year's
red/NIR series for a 418 km² AOI in 3.0 min is healthy — it is faster than campaign 1's
7.6 min for the same work.

### R2-5 (LOW, NEW) — but the threshold could not fire on a slow run either

`minutes_per_tile = elapsed_minutes / tile_total` is **wall time divided by tile count**,
while tiles are fetched concurrently on 8 workers. It is a throughput ratio, not per-tile
latency, and it is scaled down by roughly `min(tiles, workers)`.

`Z8`'s individual tiles took **107.9 s, 122.8 s, 165.9 s, 180.7 s — mean 2.4 min each** —
but because they overlapped, the metric reads **0.8**. For `Z8` to reach the 5.0 threshold
its wall time would have to be **20.0 min instead of 3.0 — a 6.7× degradation.** With 8
workers, per-tile latency would have to approach ~40 min. The failures the warning text
names (expired credentials, upstream 502s retried internally) would have to be
catastrophic before this fires.

The largest figure recorded, `Z1`'s 1.9, is a true latency only because that AOI is a
single tile — i.e. the metric is trustworthy exactly when parallelism is absent.

**What would have fired:** `farmdar.sentinel` already reports per-tile durations in
`result["results"]`. Averaging *those* and thresholding at 5.0 min would read 2.4 for `Z8`
and 1.9 for `Z1` — both correctly clear — while a genuinely retrying run reads 10–20+. If
the wall/tiles form is kept, `stac_slow_tile_warning_minutes` should be **≈ 2.0**, which
leaves both healthy runs clear and fires at a 2.5× degradation.

Related: the one genuine infrastructure failure this campaign hit — the Planetary Computer
rate limit in R2-1 — occurred in the **static window scoring** path, which has no
throughput instrumentation at all.

---

## 10. Regression

`Z2_regression_1016` — cane 2025, `okara_test_data_cane.shp`, static date pinned to
**2025-10-16**, NDVI stage bit-identical (`md5 794b121e…`) to the previous campaign's.

| run | code | static date | features | acres | delta |
|---|---|---|---|---|---|
| `A1_cane_2025` | `b013476` | 2025-10-16 | 932 | 2,120.35 | — |
| `R_cane_1016_samedate` | `c2f954d` | 2025-10-16 | 926 | **2,184.70** | +3.0% (the sieve fix) |
| **`Z2_regression_1016`** | **`ce37be8`** | 2025-10-16 | **926** | **2,184.67** | **0.0%** |

**Delta against 2,184.7 is 0.0%.** Same 926 features, same acreage. Nothing in `ce37be8`
changed the numeric product on a pinned date — as expected, since the commit's changes are
selection, write mechanics and reporting.

Stronger still: `Z8_cane_fresh_ndvi` re-ran the **entire NDVI stage from scratch** (fresh
STAC download, fresh RF inference) rather than resuming, and produced a
**byte-identical** classification map (`md5 794b121e…`) and the same **926 features /
2,184.7 acres**. The pipeline is deterministic across a full re-acquisition.

**Environment caveat, and it is a real one.** The shared `farmdar` dependency moved between
campaigns: `standard-libraries` worktree `30c67408…` → `824850c6…` ("Merge PR #26
fix/sentinel-quiet-boto-logs"). The old commit is **no longer on this box** — the clone is
shallow at `824850c` — so I could not diff the two. The commit title suggests logging only,
and the byte-identical NDVI product above is strong evidence it changed nothing numeric on
this path, but I could not verify it directly. `harness/run2.sh` exists solely because
`run.sh` still points at the deleted worktree.

---

## 11. What I could not test, and why

* **Ground truth — unchanged and still central.** I can now show spr_maize spanning
  931.7 / 3,992.8 / 8,322.0 acres with the selector's reasoning printed, but **not which
  is right.** Everything in §1 and §7 is relative. R2-4's proposed retention bounds are
  fitted to six observations on one AOI and must not be shipped as-is.
* **R2-1 is not reproducible on demand.** It depends on Planetary Computer's rate limiter.
  I observed it once, in a real run, and the code path is unconditional — but I cannot
  give a deterministic repro, and I did not measure how often it happens.
* **District/province scale.** Still one 418 km² AOI, 4 tiles. R2-5's throughput conclusion
  is about the metric's *form*, which is scale-independent; the 5.0 threshold's behaviour
  on a 200-tile district is not measured.
* **`gs://` AOIs end to end** — the sandbox still blocks writing to and enumerating the
  shared bucket, as in both previous campaigns.
* **The `farmdar` version difference** (§10) could not be diffed; the old tree is gone.
* **GEE backends** were not re-exercised. `ce37be8` touches the STAC static path,
  `raster_io`, `postprocess` and `qc`; the GEE acquisition path is unchanged, and the
  shared code below it is covered by the STAC runs.
* **A genuinely crop-absent *district*.** `Z1` is a 1,324.7-acre clipped sub-AOI selected
  from the pipeline's own output, not an independently-known crop-free district. It is
  real ground with a real zero, but the absence is the pipeline's own verdict, not an
  external fact.

---

## Appendix — scenarios

Runner `harness/run2.sh` / `queue2.sh`, specs in `specs_retest2/`, outputs in
`runs_retest/`, logs in `logs/`, metrics in `metrics/`.

| scenario | purpose | exit |
|---|---|---|
| `T1_sprmaize_margin5` | FAIL-11 default margin | 0 |
| `T2_sprmaize_margin0` | FAIL-11 margin=0 → highest coverage | 0 |
| `T3_sprmaize_margin100` | FAIL-11 margin=100 → window 1 | 0 |
| `T4_sprmaize_startat3` | FAIL-11 window 3, the old pick | 0 |
| `K1_kill_static` (+ `_resume`) | FAIL-12 SIGKILL mid-classification | killed / 0 |
| `K4a/K4b_staging_*` | R2-2 deterministic staging reuse | 0 / 0 |
| `Z1_nocane_empty` | empty result, real absence, fresh NDVI | 0 |
| `Z2_regression_1016` | regression vs 2,184.7 | 0 |
| `Z3_cane_auto` | result check — cane | 0 |
| `Z4_wheat_punjab` / `Z5_wheat_sindh` | result check — wheat, both regions | 0 |
| `Z6_noimagery_2027` | window expansion ×3 | 1 (expected) |
| `Z7_noimagery_2027_exp0` | expansion disabled, clean failure | 1 (expected) |
| `Z8_cane_fresh_ndvi` | tile throughput + full-reacquisition determinism | 0 |
| `Z9_rice2027_ndvionly` | R2-3 dataless year on the NDVI-only path | 0 |
| batch `jobs_batch_retest2.json` | empty job inside a `--stop-on-error` batch | 0 |

Probes: `retest2_window_probe.py`, `retest2_kill.sh`, `retest2_kill_writer.py`,
`retest2_prefix_control.py`, `retest2_geometry_append.py`.
