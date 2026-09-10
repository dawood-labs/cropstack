# RETEST_REPORT.md — cropstack @ `c2f954d`

Re-validation of the previous campaign's findings plus the new priority-window selector,
sensor-era policy, and field-log fixes. Same box, same harness, same AOIs as the original
campaign (`TEST_REPORT.md`).

**Subject:** `c2f954d` (was `b013476`) · **Date:** 2026-09-03
**Baseline for every "old" number:** the previous campaign's `metrics/final_validation.json`.

---

## Lead: what is still broken or newly broken

| # | Sev | Item | State |
|---|---|---|---|
| **RT-1** | **HIGH** | STAC path has no AOI geometry validation — the field-log fix landed only on the GEE grid-split path | **NEW** |
| **RT-2** | **HIGH** | spr_maize acreage swings **8.9×** across the crop's *own* priority windows; the selector makes the choice fixed, not right | **NEW** |
| **RT-3** | **MED** | Static classification is not written atomically — an interrupted run leaves a 0-byte `_Cls.tif` that a later resume dies on | **NEW** |
| **RT-4** | **MED** | FAIL-7 only partly fixed: peak 25.65 GiB vs 27.89 GiB (−8%) | **PARTIAL** |
| **RT-5** | **LOW** | FAIL-5 unchanged — a static run folder still accumulates unrelated date products | **STILL BROKEN** |
| **RT-6** | **LOW** | Auto-mode anchor warning is labelled "Manual static dates"; coverage floor printed with `:.0f` so 99.9 and 100.5 both render as "100%" | **NEW (cosmetic)** |

Everything else re-tested clean — details in §1–§5.

---

### RT-1 (HIGH, NEW) — the AOI-hygiene fix was applied to one path only

`gee_client.split_aoi_into_grid` now repairs invalid geometry, drops non-polygons, and
raises clearly on an empty AOI. The **STAC path — the default and the one the new README
recommends — got none of it.** Same AOIs, both paths:

| AOI | GEE grid split | STAC path (real `run_pipeline`) |
|---|---|---|
| bow-tie (self-intersecting) | OK, repaired, 10 cells | config OK, 4 tiles |
| polygon + line + point | OK, 4 cells | config OK, 3 tiles |
| valid polygon **+** bow-tie | OK, repaired, 18 cells | **`GEOSException: TopologyException: side location conflict at 73.428118…`** |
| empty layer | `ValueError: AOI contains no features` | **`ValueError: cannot convert float NaN to integer`** |
| points only (no polygon at all) | `ValueError: AOI has no polygon geometry after cleaning` | **config validates, run folders created, model loaded, imagery acquisition starts** |

The third row is the field log's own GeometryCollection complaint, unfixed on the path
most runs take. The last row is the worst: a points-only AOI passes `cfg.validate()` and
proceeds to download Sentinel-2 imagery — it ran for two minutes before I killed it.

`aoi_io.resolve_aoi(verify_readable=True)` accepts all five without comment, so nothing
between the user and farmdar checks geometry at all on the STAC side.

**Fix:** move the `split_aoi_into_grid` cleaning block into `aoi_io.resolve_aoi` (or into
`PipelineConfig.validate`) so both backends inherit it.
**Repro:** `harness/retest_geometry_checks.py`; specs `N_empty_aoi`, `N_real_plus_bowtie`,
`N_points_only`.

---

### RT-2 (HIGH, NEW) — priority windows make the date deterministic, not defensible

The selector works exactly as documented. The problem is what it is choosing between.
Holding the AOI, year, NDVI stage and model fixed and varying only the static date
**within spr_maize's own three configured windows**:

| static date(s) | source | features | acres | % of AOI |
|---|---|---|---|---|
| 2025-05-14 | old campaign's auto pick | 315 | **931.7** | 1.8% |
| 2025-05-09 + 2025-05-01 | **new window 1 — what the pipeline now ships** | 1,070 | **3,992.8** | 7.8% |
| 2025-05-09 alone | window 1, single date | 1,070 | 3,992.8 | 7.8% |
| 2025-04-29 | window 2 | 1,945 | **8,322.0** | 16.2% |

**8.9× between the best and worst candidate, all inside the crop's own preference list.**
Window 1 scored 99.12% coverage; window 2 scored 100.0%. The pipeline takes window 1
because it is first past the 80% floor — it never compares the two. Coverage cannot
discriminate here: every candidate scores 99–100%.

Two consequences:

* **The reported spr_maize acreage changed 4.8× between commits** (833.9 → 3,992.8) with
  no change to the NDVI stage or the model. Isolating the cause: re-running the *old*
  date under the *new* code gives 931.7 acres, so **+11.7% is code (the sieve fix) and the
  remaining 4.3× is purely the date**.
* The old finding FAIL-3/F-4 (result is extremely sensitive to static date choice) is
  **not fixed — it is now hidden**, behind a log line that reads
  `Accepted window 1/3 at 99.1% coverage (floor 80%)`. That reads like a validated
  result. It is an arbitrary one, made reproducible.

This is not an argument against priority windows — pinning the phenology is right, and
§2 shows it rescuing the 2016 case outright. It is an argument that **coverage % is the
wrong and only tie-breaker**, and that a 99.12% two-date window should not silently beat
a 100% single-date one.

**Suggested fix:** prefer a single-date window over a multi-date one at comparable
coverage; and when two windows are within a few points of each other, say so in the log
rather than taking the first. Without ground truth I cannot say which of 931.7 / 3,992.8 /
8,322.0 is correct — only that the pipeline is choosing between them on a criterion that
does not distinguish them.

**Repro:** specs `P4_sprmaize_2025`, `S1_sprmaize_0514_old`, `S2_sprmaize_0429_win2`,
`S3_sprmaize_0509_only`.

---

### RT-3 (MED, NEW) — static classification is not crash-safe

`static_classify.py:401` opens the final output path for writing directly. An interrupted
static stage therefore leaves a **zero-byte `_Cls.tif`**, and the next resume opens it and
dies:

```
rasterio.errors.RasterioIOError: '…/static_mosaic_29_Apr_2025_Cls.tif'
    not recognized as being in a supported file format.
```

Observed for real: a run of mine was killed mid-classification and the follow-up resume
failed on the corpse rather than regenerating it. The NDVI side gets this right — the
inference workers write `…_predicted.tmp.tif` and `.replace()` on completion
(`inference_workers.py:164, 247`), which is exactly why last campaign's SIGKILL test
(OK-7) resumed to a byte-identical product. The static stage never adopted the pattern.

On a box that is culled without warning — this one — that turns a routine interruption
into a run that cannot resume without manual cleanup.
**Fix:** write to `.tmp.tif` and rename, as the NDVI workers already do.

---

### RT-4 (MED, PARTIAL) — the memory cap is real but does not lower the peak here

`resolve_worker_count` now bounds the pool by cores, by window count, **and** by model
size. Verified working structurally: with the 563 MB spr_maize model it returns 4 even
when asked for 99 windows, so the old "161 GiB on a 32-core box" path is genuinely closed.

But on this box it did not bind below the window cap, and the peak barely moved:

```
Static worker pool = 4  [requested=7; windows=4]      <- memory was not the binding limit
peak tree RSS: 25.65 GiB   (old campaign: 27.89 GiB, −8%)
```

The arithmetic: 537 MiB × 12 = 6.29 GiB/worker estimated (measured resident: 5.2 GiB);
budget = 50% of 53.7 GiB free = 26.8 GiB; cap = 4 — the same 4 the window count already
imposed. **The sizing is working as designed, and the design permits a 25.65 GiB peak for
a 418 km² test AOI**, because it is willing to spend half of free RAM on identical copies
of one model.

Also worth flagging: `available_memory_bytes()` is read at pool creation, so two crops
running concurrently in a batch would each independently claim half of what they see free
and over-commit.

**Suggested fix:** cap on *absolute* model cost as well as a fraction — a model over
~1 GiB resident should force 1–2 workers regardless of free RAM, since the parallelism
buys little when `static_classify` is 5 s of a 2-minute stage.

---

### RT-5 (LOW, STILL BROKEN) — run folders still mix unrelated products

Unchanged from FAIL-5. After a manual two-date run followed by a resumed auto run,
`2_static_run_1/` holds both:

```
runs_retest/V_stale_test/2_static_run_1/
    10_Nov_2025/
    18_Oct_2025_and_10_Nov_2025/
```

The downstream danger is gone (RT/FAIL-2 below now rebuilds on raster identity), so this
is now a housekeeping wart rather than a correctness bug — but the folder still does not
identify one result.

---

### RT-6 (LOW, NEW) — two log-message defects

* In `auto` mode via priority windows the anchor warning is still prefixed
  **"Manual static dates:"** — e.g. `Manual static dates: 2025-11-10 is the ANCHOR`, on a
  run with `stac_static_mode="auto"`. It also fires for single-date selections, where
  there is no layering and no anchor to get wrong.
* The floor is printed with `{floor:.0f}`, so `stac_static_min_coverage_pct=99.9` logs as
  `floor 100%` and `100.5` logs as `the 100% coverage floor`. During this retest that
  briefly made a *passing* run look like a failing one.

---

## 1. Previous defects: are they dead?

| Old finding | Verdict | Evidence |
|---|---|---|
| FAIL-1 static sieve no-op | **FIXED** | class-1 count now changes in every run (below) |
| FAIL-2 stale vector on resume | **FIXED** | rebuild triggered on raster identity |
| FAIL-3 low-coverage scene accepted as anchor | **FIXED** | 2016: 16.9% scene rejected, walked to a 98.3% one (§2) |
| FAIL-4 `.parquet` AOI | **FIXED** | full run from `.parquet`; result identical to the `.shp` run |
| FAIL-5 mixed run folders | **STILL BROKEN** | RT-5 |
| FAIL-6 83%-cloud image classified ungated | **FIXED** | same evidence as FAIL-3 |
| FAIL-7 spr_maize 27.89 GiB | **PARTIAL** | RT-4 |
| FAIL-8 Landsat static returns 9.93 acres | **FIXED** | refused at `validate()` with the measurement in the message |
| FAIL-9 `api_manual` opaque late failure | **FIXED** | now fails client-side in 7 s |
| OBS-7 crop-mask denominator | **FIXED** | now reported as % of AOI |

**FAIL-1 — the sieve.** It now removes pixels in every run, and on the identical raster it
removes *exactly* what the old probe predicted it would if the mask were dropped:

```
old probe (FAIL-1):  115,777 -> 115,777  (delta 0)        <- no-op
                     115,777 -> 106,793  (delta -8,984)   <- predicted, if unmasked
new run  (same raster, R_cane_1016_samedate):
                     115,777 -> 106,793  (delta -8,984, -7.8%)   <- exact match
```

Across the whole retest the static sieve removes 0.7%–44.8% of class-1 (E3 −15.9%,
P1 −9.9%, P4 −11.3%, P2 −0.7%, S1 −21.5%).

**FAIL-2 — stale vector.** `vectorize_process_and_export` now fingerprints the source
raster (resolved path + size + `mtime_ns`) into a sidecar record and compares it. The
C2 scenario, rebuilt: a manual `[2025-10-18, 2025-11-10]` run wrote the vector, then a
resumed auto run recomputed the static stage. The resume logged

```
[Rebuilding] okara_cane_2025 exists but was built from a different raster
(…/18_Oct_2025_and_10_Nov_2025/…_Cls_sieved_p20.tif);
regenerating from static_mosaic_10_Nov_2025_Cls_sieved_p20.tif.
```

and returned polygons matching its own raster. Under `b013476` this returned the previous
run's product with no comment.

**FAIL-4 — `.parquet`.** `aoi_io._convert_parquet` reads the file with `gpd.read_parquet`
and writes a cached `.gpkg` that every downstream consumer can open, so the format now
works end to end rather than failing at `resolve_aoi`:

```
AOI : /home/jovyan/.cache/fao_pipeline/aoi/okara_test_data_cane_from_parquet.gpkg
      (from …/aoi_variants/okara_test_data_cane.parquet)
```

`Q_parquet_cane_2025` was a full `run_mode="new"` run — fresh STAC acquisition, fresh RF
inference, fresh static stage — and reproduced the shapefile run of the same geometry
exactly:

| run | AOI format | features | acres |
|---|---|---|---|
| `P1_cane_2025` | `.shp` (resumed NDVI) | 800 | 1,855.5 |
| `Q_parquet_cane_2025` | `.parquet` (full fresh run) | 800 | 1,855.5 |

So this both closes FAIL-4 and extends the previous campaign's format-equivalence result
(OK-5) to GeoParquet — and, incidentally, re-confirms end-to-end determinism under
`c2f954d`, since the two runs share no computation.

**FAIL-7 — see RT-4.** Worker pool logged as
`Static worker pool = 4  [requested=7; windows=4]`; peak 25.65 GiB.

**OBS-7 — crop mask denominator.** Now `Crop mask coverage: 153,043 px (6.34% of the AOI)`
where `b013476` said `3.09% of the static image grid`. 6.34% matches the independent
measurement from the last campaign (153,043 / 2,418,668 AOI pixels = 6.33%), so the
denominator is now the AOI, as intended.

---

## 2. The priority-window selector

**Which window was accepted, and how many were tried:**

| run | windows tried | accepted | date(s) | coverage |
|---|---|---|---|---|
| `P1_cane_2025` | 1 of 4 | window 1 (11-07→11-15) | 2025-11-10 | 100.0% |
| `P2_wheat_punjab_2025` | 1 of 4 | window 1 (02-10→02-25) | 2025-02-18 | 100.0% |
| `P3_wheat_sindh_2025` | 1 of 3 | window 1 (02-01→02-20) | 2025-02-03 | 100.0% |
| `P4_sprmaize_2025` | 1 of 3 | window 1 (05-01→05-10) | 2025-05-09 + 2025-05-01 | 99.1% |
| `E3_cane_2016` | **4 of 4** | window 4 (11-16→11-25) | 2016-11-17 | 98.3% |

**Order is as documented.** `resolved_static_windows()` matches the README table exactly
for cane, wheat/punjab, wheat/sindh and spr_maize; February end dates clamp correctly
(sindh window 3 → `2024-02-29` in a leap year, `2025-02-28` otherwise); an unknown region
raises `No static windows defined for region 'balochistan' on wheat. Known regions:
['punjab', 'sindh']`.

**The 2016 walk is the selector earning its keep.** This is the exact scene that produced
FAIL-6:

```
window 1/4 (2016-11-07..11-15): ['2016-11-14'] -> 16.9% of AOI usable       <- FAIL-6's image
window 2/4 (2016-11-01..11-06): could not be scored (no scenes < 80% cloud); next window
window 3/4 (2016-10-15..10-31): ['2016-10-25','2016-10-15'] -> 8.9%
window 4/4 (2016-11-16..11-25): ['2016-11-17'] -> 98.3%
Accepted window 4/4 at 98.3% coverage (floor 80%).
```

The old code took 2016-11-14 at 17.15% and classified 83% cloud as ground. The new code
rejects it and finds a clean image the old selector never looked at. Reported acreage
moves 768.5 → 2,096.1, and the second number rests on a 98.3%-clear scene.

**Scoring does not download imagery — confirmed.** Measured on an idle box (ambient noise
0.002 MB/s), scoring one window costs:

| crop | per-window time | per-window network |
|---|---|---|
| cane | 3.6–7.8 s | 0.41–1.09 MB |
| wheat/punjab | 2.6–3.7 s | 0.52–1.10 MB |
| spr_maize | 2.9–3.3 s | 0.52–0.92 MB |

Against ~2,500 MB for a real STAC static acquisition, that is **three orders of magnitude
below** the imagery cost — metadata plus a coarse SCL read, as documented.

*(An earlier attempt to measure this from a full run's samples was contaminated: 822 MB
had already been received before the scan began, because a GEE run was executing
concurrently. `psutil.net_io_counters()` is system-wide. The numbers above are from a
dedicated probe on an otherwise idle box.)*

**Forced fallback works.** With `stac_static_min_coverage_pct=100.5` (unreachable) the
cane run walked all four windows in order and then warned:

```
window 1/4 …100.0%   window 2/4 …100.0%   window 3/4 …100.0%   window 4/4 …100.0%
WARNING No window reached the 100% coverage floor. Falling back to the best available:
        window 1/4 (2025-11-07 to 2025-11-15) at 100.0% -- the result rests on partly
        cloudy or partly covered imagery.
… STAC static dates (auto mode): ['2025-11-10'] via window 1/4 … (below floor)
```

Correct behaviour, and the `(below floor)` tag propagates into the run description. Note
my first attempt used `99.9`, which did **not** force a fallback because coverage was
exactly 100.0 — and the log rendered the floor as "100%" either way (RT-6).

**Cost of scanning.** In the common case exactly one window is scored, so the selector
adds **~3–4 s** before acquisition. The two full four-window walks observed:

| run | windows scored | scan wall clock | acquisition that followed |
|---|---|---|---|
| `E3_cane_2016` (real rejections) | 4 | ~11.4 s (13:01:56 → 13:02:07.8) | 56.8 s |
| `P5_cane_fallback` (forced) | 4 | ~14.5 s (13:02:55 → 13:03:09.7) | 10.2 s (warm cache) |

So a worst-case walk costs 11–15 s. On the 2016 run that is ~20% added to the static
acquisition stage, in exchange for not classifying an 83%-cloud image — plainly worth it.
On a warm-cache run the scan can exceed the acquisition it precedes, but both are small
against the ~7 min NDVI stage.

**Acreage vs the old single-window auto:**

| case | old date | old acres | new date | new acres | change |
|---|---|---|---|---|---|
| cane 2025 | 2025-10-16 | 2,120.3 | 2025-11-10 | 1,855.5 | **−12.5%** |
| wheat 2025 (punjab) | 2025-02-03 | 15,475.7 | 2025-02-18 | 18,404.3 | **+18.9%** |
| wheat 2025 (sindh) | 2025-02-03 | 15,475.7 | 2025-02-03 | 15,750.8 | +1.8% |
| spr_maize 2025 | 2025-05-14 | 833.9 | 05-09 + 05-01 | 3,992.8 | **+378.8%** |
| cane 2016 | 2016-11-14 (17% clear) | 768.5 | 2016-11-17 (98% clear) | 2,096.1 | +172.7% |

**Explanation of the divergences.** The wheat/sindh row is the control: its window 1
happens to select the same date the old auto mode picked, and the result moves only
**+1.8%** — that is the pure code delta (chiefly the sieve fix). Every large divergence is
therefore a *date* change, not a code regression, and each is attributable:

* **cane −12.5%**: the priority list puts 7–15 Nov ahead of 15–31 Oct, so 2025-11-10
  replaces 2025-10-16. The old campaign measured 2025-11-10 independently at 1,817.9
  acres, consistent with the 1,855.5 here.
* **wheat +18.9%**: window 1 (10–25 Feb) selects 2025-02-18 instead of 2025-02-03, which
  now falls in window 2.
* **cane 2016 +172.7%**: the old number was computed over an 83%-cloud image. This
  divergence is a *fix*, not a drift.
* **spr_maize +378.8%**: see **RT-2**. This one I cannot defend, and it is the reason the
  retest is not a clean pass.

---

## 3. Sensor eras

| Requirement | Result |
|---|---|
| 2014 cane → GEE/Landsat NDVI, static off, vector produced | **PASS** — `cane 2014: before Sentinel-2, so NDVI comes from GEE/Landsat 8`; GEE asset `okara_test_data_cane_2014_Landsat`; `static_source=None`, `static_run=null`; 1,116 features / 3,193.1 acres in 3.8 min |
| 2014 + `ndvi_source="stac"` → clear refusal | **PASS** — `ValueError: ndvi_source='stac' but Sentinel-2 does not cover 2014 (archive starts 2016). Use ndvi_source='gee', which falls back to Landsat 8 for pre-Sentinel-2 years.` |
| 2016 cane, GEE NDVI + STAC static → runs, report acreage | **PASS** — Landsat NDVI (asset `…_2016_Landsat`) with a Sentinel-2 static image from 2016-11-17; **778 features / 2,096.1 acres**; the static side ran the full window walk (§2) |
| 2016 + `static_source="gee"` → NDVI-only, no Landsat static | **PASS** — `WARNING cane 2016: static image would come from Landsat 8, which the static model cannot use -- running NDVI-only`; summary shows `static source: gee (api_auto) [DISABLED: run_static_model=False]`, `static_run=null` |

Forcing the refused combination raises rather than silently degrading:
`run_static_model=True` on a Landsat year gives a `ValueError` that names the cause (no
red-edge band, `(red+NIR)/2` substitute, "measured 77× below the Sentinel-2 result") —
i.e. the previous campaign's FAIL-8 measurement is now encoded in the guard.

---

## 4. Field-log fixes

**Tile completeness — PASS (both branches).** Stubbing farmdar's `fetch_sentinel_imagery`
return value against the real acquired tiles:

| case | result |
|---|---|
| control: 4 tiles present, all reported ok | returns 4 tiles (no false positive) |
| one tile reported `failed: HTTPError 503` | `RuntimeError: STAC acquisition failed for 1 of 4 tile(s): [('0003', 'failed: HTTPError 503')]. Re-run with run_mode='resume'…` |
| one tile file missing from disk | `RuntimeError: STAC acquisition returned 3 tile file(s) but reported 4 tile(s). The mosaic would have holes.` |

The run refuses rather than mosaicking short, which is what was asked.

**GeometryCollection — PASS on the GEE grid split, FAIL on the STAC path.** See **RT-1**.
`split_aoi_into_grid` repairs a self-intersecting bow-tie (`Repairing 1 invalid AOI
geometr(ies) before gridding`) and a mixed polygon/line/point layer, and grids both. The
STAC path crashes with a raw `GEOSException` on the same messy AOI.

**Empty and non-polygon AOI — PARTIAL.** Clear on the GEE path (`AOI contains no
features` / `AOI has no polygon geometry after cleaning`); on the STAC path an empty AOI
gives `ValueError: cannot convert float NaN to integer` and a points-only AOI is not
rejected at all. This is exactly the "no NoneType/schema failures" requirement, met on one
path and missed on the other.

---

## 5. Regression

`P1_cane_2025` (cane 2025, STAC NDVI + STAC static, auto) is **not** byte-identical to
the previous campaign, and the priority windows are the reason: it selects **2025-11-10**
where the old wide-window auto selected **2025-10-16**.

Pinning the date isolates the code change from the date change:

| run | code | static date | features | acres |
|---|---|---|---|---|
| `A1_cane_2025` (old campaign) | `b013476` | 2025-10-16 | 932 | 2,120.35 |
| `R_cane_1016_samedate` | `c2f954d` | 2025-10-16 | 926 | **2,184.7** |
| `P1_cane_2025` | `c2f954d` | 2025-11-10 (window 1) | 800 | 1,855.5 |

**Same date, new code: +3.0%** (932→926 features, 2,120.35→2,184.7 acres). That delta is
the sieve fix: the static sieve now genuinely removes small blobs from the raster
(−8,984 px here), which changes which polygons survive the area filter. It is expected and
explained, not a regression — but it does mean **no scenario in this campaign reproduces
the previous one byte-for-byte**, and any stored baseline acreage from `b013476` should be
regarded as superseded.

The NDVI stage *is* unchanged: every retest run resumed the previous campaign's NDVI
products, and the wheat/sindh control moved only +1.8%.

---

## 6. What I could not test, and why

* **Ground truth.** Unchanged from the last campaign and central to RT-2: I can show that
  spr_maize ranges 931.7 → 8,322.0 acres across its own windows, but not which value is
  right. Every accuracy statement here is relative.
* **`gs://` AOIs end to end** — the sandbox still blocks writing to and enumerating the
  shared bucket, as in the previous campaign.
* **District/province scale** — still a single 418 km² AOI. RT-4's memory conclusion is
  therefore about the model term, which is AOI-independent; the raster term is not
  exercised at scale.
* **The original FAIL-9 route is now unreachable**, so it could not be re-tested as
  written: the Landsat-static guard (FAIL-8) fires at `validate()` before
  `build_static_composite` is reached. I tested the new client-side date check on a
  supported year instead (2025, `api_manual`, 2025-11-11 — a day with no overpass) and it
  raised `No SENTINEL acquisition on 2025-11-11 over this AOI` in **7 s**, before any
  export was submitted.
* **Concurrency contamination.** Several retest runs were executed while a GEE run was in
  flight to save wall clock. Process-tree RSS and stage timings are isolated, but
  `psutil` network counters are system-wide; every network figure quoted in §2 is from a
  dedicated probe run on an idle box, not from those overlapping runs.
* **The 0-byte-raster failure in RT-3 was first triggered by my own killed run**, not by
  cropstack. I have reported it because the write path that allows it is unconditional,
  and this instance is culled without warning — but the trigger in evidence is mine.
