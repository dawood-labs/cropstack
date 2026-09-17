# TEST_REPORT_2026-09-16.md — setup + audit of `cropstack` @ `73198a3`

> **Update, same day:** the user supplied a real AOI
> (`/home/jovyan/testing_cane/fao_cane_validation_aoi_1/fao_cane_validation_aoi_1.shp`)
> and a real GEE/GCS service-account key, and `farmdar.sentinel` was located already
> installed on this machine (a shared, read-only worktree — not copied or modified). All
> three gaps in §4 below are now closed. **§5** covers the live verification this made
> possible: real pipeline runs, live reproductions of RT-1, and several new findings that
> only a live run could surface. Sections 1-4 are left as originally written (the
> offline/static-only audit); treat §5 as the authoritative update where the two disagree
> — everything in §5 was directly observed, not inferred.

Setup: `git clone https://github.com/dawood-labs/cropstack.git` (equivalent of `gh repo
clone`; `gh` is not installed on this box, confirmed via `which gh` → not found, and
`git ls-remote` confirmed the repo is reachable over plain HTTPS). Python 3.11.15 venv,
`pip install -r requirements.txt` — clean install, no conflicts.

**Hard-dependency gap, as expected going in:** `farmdar.sentinel` is not importable
(`ModuleNotFoundError: No module named 'farmdar'`), confirmed by direct import attempt.
This is the package `requirements.txt` itself documents as "synced read-only from the
farmdar repo" — not part of this clone. No AOI shapefile with a full sidecar set, no
GEE/GCS service-account key, and no `gs://` bucket access are available either. Per
instructions, nothing here was stubbed or mocked to force a run through; every finding
below was produced by static code reading, code-path tracing, and small offline scripts
that exercise `config.py` / `resources.py` / `postprocess.py` logic directly (no network,
no farmdar, no credentials). See §4 for exactly what a live run would still need.

---

## 1. Baseline: offline test suite

```
python tests/run_all.py
```

**81 passed, 0 failed, exit code 0.** (First run reported exit 1 only because `tee` was
piping to a scratch directory that didn't exist yet — the suite itself printed `81
passed, 0 failed`; re-run with clean redirection confirmed exit code 0.)

Suite composition: `test_qc_retention`, `test_acquisition_reporting`, `test_pool_recycle`,
`test_resources`, `test_window_model_match` (5 files, all offline/mocked, no network or
farmdar dependency — confirmed by reading each file's imports).

One test-hygiene note, not a pipeline defect (flagged in the brief and confirmed): the
resource-planner test asserting "unknown RAM does not crash the planner" depends on the
host's actual free RAM being reported as a specific number in its log message rather than
on behavior — it passed here, but it is fragile on a memory-constrained box. Not re-logged
below as a new pipeline defect.

---

## 2. Status of every previously-logged issue

Checked against current `HEAD` (`73198a3`) by reading the actual code (not by re-running
the campaigns, since farmdar/credentials aren't available). "CONFIRMED" below means I read
the exact code path myself, not that I re-ran the scenario.

| ID | Issue | Prior status | **Current status, verified against `73198a3`** |
|---|---|---|---|
| FAIL-1 | Static sieve no-op (`nodata_val`=background label masks out 97.7% of the raster) | FIXED (retest 1) | **CONFIRMED FIXED.** `static_pipeline.py:541` passes `nodata_val=None` to the static sieve; `postprocess.py:114-119` branches correctly on `None` (whole raster participates) vs. a real nodata value. |
| FAIL-2 | `run_mode="resume"` returns a stale vector product from different raster data | FIXED (retest 1) | **CONFIRMED FIXED.** `postprocess.py:63-77, 226-266` fingerprints the source raster (resolved path + size + `mtime_ns`) into a `.source.json` sidecar and rebuilds whenever it doesn't match, logging `[Rebuilding] ... exists but was built from a different raster`. |
| FAIL-3 | Low-coverage/swath-edge static scene accepted silently as the layering anchor | FIXED (retest 1, via coverage gate) | **CONFIRMED FIXED** for the gating half — `_check_static_coverage` (`static_pipeline.py:28-48`) is called on every acquisition path and warns/errors below `stac_static_min_coverage_pct`. The **anchor-order ambiguity itself is only half-documented**, see new finding N-1 below. |
| FAIL-4 | `.parquet` AOIs advertised but never work | FIXED (retest 1) | **CONFIRMED FIXED.** `aoi_io._convert_parquet` (`aoi_io.py:137-156`) reads with `gpd.read_parquet` and caches a `.gpkg` every downstream consumer can open. |
| FAIL-5 | A resumed static run folder accumulates unrelated date products | **STILL BROKEN** (retest 1 + retest 2, unchanged) | **STILL BROKEN, confirmed.** `static_pipeline.py:487-499`: the code now detects the situation and logs a `logger.warning(...)`, but does **not** prevent or clean up the accumulation — it just tells the operator to pass `static_run_mode="new"` themselves. Same underlying behavior as originally logged, now with a visible symptom instead of a silent one. |
| FAIL-6 | 83%-cloud static image classified with no gate | FIXED (retest 1) | **CONFIRMED FIXED** — same `_check_static_coverage` gate as FAIL-3. |
| FAIL-7 | `static_worker_count` sized from cores, ignores model size (27.9 GiB peak) | PARTIAL (retest 1, RT-4) | **STILL PARTIAL, same shape.** `static_classify.resolve_worker_count` (`static_classify.py:87-140`) now caps the pool by `model_bytes = disk_size × 12.0` (a fixed multiplier, not measured per-model — real measured expansions ranged 9.2×-15.7× across crops) **and** by window count **and** by cores, taking the minimum. On a small AOI the window-count cap binds first, so the fix is real but doesn't move the peak on the AOIs anyone has actually measured — exactly RT-4's finding, unchanged. Compounded by new finding **N-2** below (batch.py/resources.py resource-planning mismatch), which independently starves or over-allocates this same pool depending on batch size. |
| FAIL-8 | Pre-2018 GEE/Landsat static path returns an implausible result (77× low) | FIXED (retest 1) | **CONFIRMED FIXED.** `config.py:576-585`, `validate()` raises `ValueError` naming the measured 77× discrepancy before any acquisition starts. |
| FAIL-9 | `api_manual` accepts a GEE date with no scene, fails opaquely ~20s later after export submission | FIXED (retest 1) | **CONFIRMED FIXED.** `gee_client.py:404-410`: `collection.size().getInfo() == 0` is checked client-side, before `.mosaic()`/export, raising `ValueError: No {sensor_mode} acquisition on {date} over this AOI` immediately. |
| OBS-6 | Misleading `Unknown PipelineConfig field(s): ['aoi_shapefile']` when both AOI spellings are passed | FIXED | **CONFIRMED FIXED.** `config.py:704-709` now explicitly detects "both passed" and raises a clear `TypeError` naming both values. |
| OBS-7 | Degenerate-crop-mask guard measured against the bounding box, not the AOI | FIXED (retest 1) | **CONFIRMED FIXED.** `static_classify.py:249-253` (`_estimate_aoi_pixels`) measures against the AOI polygon's own area via a dynamically-computed local UTM zone (see finding **N-3** — this same correct technique is *not* used for the pipeline's headline acreage numbers). |
| OBS-8 / RT-6 | Two log-message defects: (a) auto-mode anchor warning wrongly prefixed "Manual static dates:", firing even for single-date selections with nothing to anchor; (b) coverage floor printed with `{:.0f}`, so 99.9% and 100.5% both render as "100%" | NEW at retest 1, not listed as closed at retest 2 | **PARTIALLY FIXED.** (a) is fixed for auto/priority-window mode — the "Manual static dates:" line (`static_pipeline.py:388`) is now only reached in the genuine `stac_static_mode=="manual"` branch. **But** it still fires unconditionally inside that branch even when there is only one manual date (no layering, no real anchor) — the exact sub-case the original finding called out is still present, just narrowed to fewer runs. (b) is **unfixed**: `static_pipeline.py:216, 221, 225` still format the floor with `{floor:.0f}%`. |
| RT-1 | STAC path (the default, and the one README recommends) has no AOI geometry validation — only `gee_client.split_aoi_into_grid` repairs invalid geometry / rejects empty or non-polygon AOIs | NEW at retest 1, not listed as fixed at retest 2 | **CONFIRMED STILL BROKEN.** `aoi_io.resolve_aoi` (`aoi_io.py:211-275`) validates only: path exists, shapefile sidecars present, and `gpd.read_file(..., rows=1)` succeeds. It does **not** check `is_valid`, does not drop non-polygon geometries, and does not reject an empty AOI (beyond the parquet-specific empty check at line 152-153). The repair/clean logic that exists (`gee_client.py:109-115`, `make_valid()` + `explode()`) is only reachable from the GEE grid-split path. `ndvi_pipeline.py:174` (`_stac_worker_budget`) and `_acquire_static_from_stac`/`_acquire_static_from_gee`'s STAC branch both hand the raw, unvalidated AOI straight to `farmdar.sentinel`. See finding **N-4** for one specific consequence I could verify without farmdar. |
| RT-2 / FAIL-11 | First-past-the-post window selection hides how sensitive the result is to which date is chosen (8.9× spread across spr_maize's own preference list) | FIXED at retest 2 (all windows now scored and compared; margin lever added) | **CONFIRMED the mechanism is fixed** — `static_pipeline.select_dates_by_priority` (`static_pipeline.py:112-253`) scores every configured window, logs the full table and the coverage spread, and only then picks by preference-margin. This makes the sensitivity visible and operator-controllable (`static_window_preference_margin_pct`, `static_window_start_at`) rather than hidden — it does not and cannot reduce the underlying date-sensitivity itself, which is a property of the imagery, not the code. |
| RT-3 / FAIL-12 | Static classification/mosaic/sieve not written atomically — an interrupted run leaves a corrupt/0-byte file a resume dies on | FIXED at retest 2 | **CONFIRMED FIXED across all three.** `static_classify.py:438,463` writes to `{output_path}.tmp.tif` then `os.replace()`; `raster_io.py:152,161` (mosaic) and `postprocess.py:174,181` (sieve) follow the identical pattern, matching the NDVI tile workers' existing discipline (`inference_workers.py`). |
| RT-4 | Memory cap real, but on the tested AOI the window-count cap binds first so peak barely moves; design still permits half of free RAM in identical model copies; two districts run concurrently would each independently claim half of what they see free | PARTIAL at retest 1 | **Same partial state, unchanged** — see FAIL-7 above. The concurrent-district over-commit half of RT-4 is now moot for `batch.py` specifically, but only because of a *different* bug (**N-2**): batch.py never actually runs districts concurrently, so the over-commit RT-4 warned about doesn't trigger there — but the planner's `districts_in_parallel` concept is still live for anyone driving `plan_resources` directly with concurrency of their own, and that path is untested. |
| RT-5 | Run folders still mix unrelated date products (housekeeping, not correctness, since FAIL-2's fix makes the vector stage rebuild correctly regardless) | STILL BROKEN at retest 2 | **CONFIRMED STILL BROKEN**, same as FAIL-5 above — now warned about, not prevented. |
| RT-6 | See OBS-8 | New at retest 1 | See OBS-8 above — partially fixed. |
| R2-1 | A transient STAC/Planetary-Computer rate limit silently drops the leading window from the comparison; run proceeds on a worse window, 2.1× the acreage, exit 0 | FIXED (commit `17a2e10`, verified live in `c1e4c03`) | **CONFIRMED FIXED.** `_score_window` (`static_pipeline.py:51-88`) retries with backoff (`static_window_score_attempts`, `static_window_score_retry_seconds`) and returns a `status="unscored"` record rather than dropping the window; `select_dates_by_priority` (lines 169-176, 232-248) raises when an unscored window outranks the chosen one (or warns, if `static_window_on_score_error="warn"`). |
| R2-2 | Static staging tiles are anonymous on disk and get reused across different requested dates, mosaicking the wrong imagery under the new date's name/folder/provenance | FIXED (`17a2e10`, verified `c1e4c03`) | **CONFIRMED FIXED.** `STAGING_RECORD`/`_staging_identity`/`_prepare_staging`/`_record_staging` (`static_pipeline.py:256-315`) fingerprint staging by source, dates, AOI, bands, resolution and tile size, and discard-and-redownload whenever that disagrees, logging which dates the stale tiles belonged to. |
| R2-3 | NDVI stage doesn't notice a year with no imagery at all; on an NDVI-only crop this now produces a clean "0 acres" product indistinguishable from a real absence | FIXED (`17a2e10`) | **CONFIRMED FIXED.** `ndvi_pipeline.py:320` raises `RuntimeError: ... is nodata in every pixel` before classification; `postprocess.py:284-297` applies the identical guard on the vectorization side, independent of which stage's raster is being vectorized. |
| R2-4 | `qc_min/max_static_retention_pct` defaults (5.0/99.5) are far too wide for real data, and the floor false-positives on genuinely crop-free ground with a wrongly-worded diagnosis | FIXED (bounds removed to `None` per the maintainer's own session notes; wording corrected) | **CONFIRMED FIXED.** `config.py:328-329` defaults both bounds to `None` (no built-in plausible range at all now — an explicit design choice, documented in the field-facing comment). `qc.py:126-134` no longer asserts a cause for the 0%/100% degenerate cases ("the two look the same from here... worth confirming"), which is the exact false-positive wording R2-4 flagged. |
| R2-5 | `stac_slow_tile_warning_minutes` applied to wall-clock/tile-count, a throughput ratio scaled down by worker concurrency, so it can't fire on a genuinely slow run | FIXED | **CONFIRMED FIXED** — the test suite itself demonstrates this (`test_acquisition_reporting`: "real run: per-tile cost comes from farmdar's own durations... 2.65 min/tile, not the 0.8 the old wall/tiles form gave"). A `per_tile_minutes()` function using farmdar's own reported per-tile durations exists in `ndvi_pipeline.py` and is what `stac_slow_tile_warning_minutes` is now checked against. |

**Session-notes item, re-examined and found to be stale (not re-logged as new):**
`CONTEXT.md` §5 lists an open task, "make the crop mask windowed — right now the whole
AOI is built at once," attributed to the static-classify memory investigation. Reading
`static_classify.build_crop_mask` (`static_classify.py:211-307`) shows it has iterated in
`chunk_size`-sized `Window` blocks — reading, rasterizing and writing one block at a
time — since the very first commit that introduced this module (`git log -S "def
build_crop_mask"` → `645fcea`, the initial unified-pipeline commit). It was never
whole-AOI. Either this note was written about a different function, or it predates a fix
that has been in place since day one; either way, the crop-mask stage's 3.8 GiB measured
peak on Kasur is not explained by "unwindowed," and whoever picks this up next should
look elsewhere (candidates: `WarpedVRT`'s own internal block cache, or the AOI vector
data held fully in memory via `geometries = aoi.geometry.values` for a very complex
multi-polygon AOI) rather than re-deriving the windowing that's already there.

---

## 3. New findings

### N-1 (MEDIUM) — the GEE static anchor-order hazard (FAIL-3's sibling) is still undocumented, and validate() doesn't catch the case where it silently overrides itself

**File/line:** `config.py:606-618` (`validate()`), `static_pipeline.py:421-424`
(`_acquire_static_from_gee`), `gee_client.py:389-420` (`build_static_composite`).

**What happens.** `gee_static_mode="api_manual"` accepts *either* `gee_static_single_date`
*or* both `gee_static_top_date`+`gee_static_bottom_date` — but nothing stops a caller from
setting **all three at once**. `validate()` only checks `has_single or has_pair`
(`config.py:610-616`); it never checks `has_single and has_pair` as a contradiction.
Downstream, `composite_type = "mosaic" if (manual_mode and cfg.gee_static_top_date) else
"single"` (`static_pipeline.py:424`) looks at `top_date` alone — if it's truthy, the whole
run silently becomes a two-date mosaic and `gee_static_single_date` is dropped with no
warning anywhere.

**Verified directly** (no GEE/GCS credentials needed — this is pure config logic, tested
with a dummy AOI and a dummy key-file path so `validate()`'s file-existence checks pass):

```
TEST 1 (both single_date and top/bottom set): validate() PASSED SILENTLY -- no contradiction error raised
  -> composite_type resolves to 'mosaic'; gee_static_single_date='2025-11-10' is SILENTLY IGNORED: True
```

**Why it matters.** This is exactly the pattern the user flagged for
`gee_static_single_date`/`top_date`/`bottom_date` (FAIL-3's anchor issue) — a "pick one of
N" field group where the winning field is inferred by silent precedence instead of an
explicit, validated choice. An operator who sets `gee_static_single_date` for a fallback
and later adds `gee_static_top_date`/`bottom_date` for a different scenario, without
remembering to clear the first, gets no error and no log line — just a different image
than the one the single-date field implies.

**Suggested fix:** in `validate()`, raise when `has_single and has_pair` are both true,
the same way `ModelSource.validate` already refuses "set exactly one of."

---

### N-2 (MEDIUM-HIGH) — `batch.py` asks `resources.plan_resources` for concurrent-district sizing, then runs every job sequentially, under-provisioning every job in a batch

**File/line:** `batch.py:138-139`, `resources.py:99-186`.

**What happens.** `batch.py`'s own docstring is explicit: *"Jobs run sequentially — each
one already saturates the machine with its own worker pool, so running two at once would
just contend for RAM"* (`batch.py:25-28`), and `run_batch` is a plain `for` loop over jobs
with no threading, multiprocessing, or async — confirmed by reading the whole function
(`batch.py:116-189`). Yet at line 139:

```python
if plan is None and auto_resources:
    plan = resources.plan_resources(district_count=len(jobs))
```

`plan_resources(district_count=N)` (`resources.py:99-186`) treats `N` as *districts to run
concurrently* and divides cores and memory accordingly:
`by_cores = max(1, cores // 2)`, then `parallel = min(district_count, by_cores or
by_memory)`, then `cores_each = cores // parallel`, and each job's `ndvi_worker_count` /
`static_worker_count` is set to `cores_each` (or `cores_each - 1` only when `parallel==1`).

Concretely, on the 8-core/61 GiB box these campaigns were run on (confirmed by the test
suite's own fixtures: *"ten districts on 8 cores / 61 GiB run 4 at a time... each gets its
own core share -- 2"*), a `batch.py` run of 10 jobs gets `districts_in_parallel=4` and
each of the 10 **sequential** jobs is handed `ndvi_worker_count=2, static_worker_count=2`
— instead of the single-district default of 7 (`cores - 1`) it would get running alone.
Every job in the batch runs at roughly 2/7 of the achievable parallelism, with 5-6 idle
cores, for the entire batch — a real, silent, multi-x slowdown of the tool's own advertised
"many districts at once" workflow, not a correctness bug but a significant, previously
unflagged design/performance defect.

`run.py`'s `--districts N` flag (`run.py:59-61,83-85`) is the one place this planner is
used *correctly* — it's documented there as "how many districts **you intend to run in
total**" for an operator who is themselves fanning out separate `run.py` invocations
concurrently (e.g. from an external scheduler). `batch.py` has no such external
concurrency and should not be sizing as if it did.

**Suggested fix:** `batch.py` should call `resources.plan_resources(district_count=1)` (or
pass `districts_in_parallel=1` explicitly), since it is the one calling context in this
codebase that is guaranteed to never run more than one district's worker pool at a time.

---

### N-3 (MEDIUM-HIGH) — the pipeline's headline acreage is computed in a CRS hardcoded to Pakistan's UTM zone 42N, while a different part of the same codebase already computes area correctly for any AOI location

**File/line:** `postprocess.py:36,319,354,356`, `qc.py:26,35` vs. `static_classify.py:180-184`.

**What happens.** `postprocess.py` and `qc.py` each independently define:

```python
AREA_CRS_EPSG = 32642   # "Pakistan-wide AOIs; matches the original notebooks' hardcoded area CRS"
```

and use it for every acreage figure the pipeline reports: the pre-clip area filter and the
final `area_acres`/`crop_acres` in the exported polygons (`postprocess.py:319,356`), and
`aoi_acres` in `result_check.json` (`qc.py:35`). Every headline number this pipeline
produces is computed by reprojecting the AOI/polygons into a **fixed** UTM zone whose
central meridian is 69°E, regardless of where the AOI actually is.

**The codebase already knows how to do this correctly**, just not where it's used for
reporting. `static_classify._estimate_aoi_pixels` (`static_classify.py:174-198`) computes
a **per-AOI** UTM zone from the AOI's own centroid —
`utm_zone = int((centroid.x + 180) / 6) + 1`, hemisphere from `centroid.y >= 0` — and
reprojects into that zone for its own internal pixel-count estimate. This function is only
used for the degenerate-mask-coverage check and pixel-count estimation; it is never applied
to the numbers a user actually reads.

**Why it matters.** UTM areal distortion grows with distance from the zone's central
meridian. Within Pakistan proper (roughly zones 41N-43N, ~61°E-77°E) the error at the
edges of zone 42N is real but modest; the practical risk is (a) an AOI in Balochistan near
61-64°E or Punjab/GB's eastern edge near 74-77°E, both several degrees outside zone 42N's
ideal range, and (b) this is an FAO-branded, source-agnostic pipeline
(`README.md:1`, "FAO Crop Mapping Pipeline") whose config takes a bare `aoi_path` with no
geographic restriction — nothing stops it from being pointed at an AOI anywhere else in the
world, where EPSG:32642 would be badly wrong (potentially even the wrong hemisphere's
northing convention). Nothing in `validate()` restricts or warns about AOI location either.

**Suggested fix:** derive the area-computation CRS the same way `static_classify.py` already
does — from the AOI's own centroid, computed once per run — instead of a fixed EPSG code
duplicated in two files. At minimum, note the Pakistan-only assumption in a place `validate()`
can check, so an AOI outside the intended zone fails loudly rather than reporting a subtly-off
acreage.

---

### N-4 (LOW, corroborates RT-1) — the STAC path's own worker-budget estimator now silently swallows the exact NaN crash RT-1 documented, without fixing the underlying gap

**File/line:** `ndvi_pipeline.py:158-207` (`_stac_worker_budget`).

`RETEST_REPORT.md` documented an empty AOI on the STAC path raising `ValueError: cannot
convert float NaN to integer` (an empty `GeoDataFrame.total_bounds` is `[nan, nan, nan,
nan]`, and `math.ceil(nan)` raises `ValueError`). `_stac_worker_budget` performs exactly
this computation — `bounds = gpd.read_file(cfg.aoi_path).to_crs(4326).total_bounds`,
`math.ceil((bounds[2]-bounds[0]) / cfg.stac_tile_size_deg)` — but wraps it in a bare
`except Exception: return workers` (`ndvi_pipeline.py:178-179`). This means a `ValueError`
from a degenerate AOI here is now caught and ignored, and the function silently falls back
to the default worker count rather than crashing *or* rejecting the AOI. It's consistent
with RT-1 remaining open: no validation was added, only one incidental crash site was
quieted. I could not confirm whether the exact error RETEST_REPORT quoted originated here
or somewhere inside `farmdar.sentinel` itself (unavailable in this clone) — but either way,
an empty/invalid AOI on the STAC path still proceeds toward acquisition with no rejection
anywhere before `farmdar.sentinel` is called, which is the actual RT-1 finding.

---

### N-5 (LOW) — `region` is accepted and silently ignored for crops that don't define regional windows, including nonsense values

**File/line:** `config.py:489-503` (`resolved_static_windows`).

`if self.region and self.static_priority_windows_by_region:` — only `wheat` populates
`static_priority_windows_by_region`; every other crop has `{}` there, which is falsy, so
the whole condition short-circuits and `region` is silently dropped with no warning,
**even for a nonsense value**. Verified directly:

```
cane + bogus region -> resolved_static_windows() first window: ('2025-11-07', '2025-11-15')
no error, no warning raised for a region value that means nothing to cane
```

An operator who copies a wheat job template (`region="sindh"`) for a cane job, or typos a
region name, gets no feedback that the field did nothing. Cheap fix: warn (not error, since
it's a legitimate no-op for non-regional crops) when `region` is set but
`static_priority_windows_by_region` is empty, or has no such key even though non-empty.

---

### N-6 (LOW) — `stac_static_mode="auto"` silently ignores `stac_static_dates` when the crop defines priority windows (which all shipped crops do)

**File/line:** `static_pipeline.py:326-343`.

Setting `stac_static_dates` has an effect only in `stac_static_mode="manual"`, or in
`"auto"` mode for a crop with **no** `static_priority_windows` configured (none of the
shipped crops qualify — `resolved_static_windows()` is non-empty for cane, wheat,
spr_maize; rice/cotton skip the static stage entirely). For every crop that ships today,
setting `stac_static_dates` while `stac_static_mode="auto"` is a silent no-op. Verified:

```
TEST 2 (auto mode + stac_static_dates set): validate() PASSED SILENTLY
  -> resolved_static_windows() non-empty: True -- so stac_static_dates=['2025-01-01'] is SILENTLY IGNORED
```

Not a correctness bug (the field genuinely has no effect in that mode, by design), but a
"sets a field, nothing happens, nothing says so" gap in the same family as N-1/N-5. Worth a
one-line warning in `build_pipeline_config` or `validate()` when both are set together.

---

### N-7 (LOW, test-coverage gap, not a pipeline defect) — the persisted test suite has zero coverage of `config.py`'s `validate()` or of AOI geometry handling

`tests/run_all.py`'s five files (`test_qc_retention`, `test_acquisition_reporting`,
`test_pool_recycle`, `test_resources`, `test_window_model_match`) cover retention/QC
reporting, acquisition-cost reporting, worker-pool recycling, resource planning, and
NDVI-model/window matching — 81 checks total, and all genuinely useful. **None of them
exercise `PipelineConfig.validate()`** for any source/mode combination, and none touch
`aoi_io.py` at all. The ad-hoc investigation scripts that *did* probe config validation
during the earlier campaigns (`harness/config_tests.py`, `harness/retest_config_checks.py`,
`harness/retest_geometry_checks.py`) were never promoted into `tests/`, are not run by
`run_all.py`, and are not reproducible in this environment as written (they hardcode a
different machine's absolute paths: `/home/jovyan/FAO/optimized_code_testing/...`). This
is exactly the area of the codebase this audit was asked to focus on (§ "every
source/mode combination... whether validate() actually catches every invalid
combination"), and it is the one area with no regression protection at all — every finding
in N-1, N-5, N-6 above would have been caught by a handful of `validate()`-only unit tests
that need no farmdar, no credentials, and no network.

---

## 4. Open questions — inputs needed to go further

I did not stub, mock, or bypass any of the following, per instruction; everything above was
produced without them. To extend this audit into an actual run, I would need:

1. **The `farmdar` package** (`farmdar.sentinel`), so that `ndvi_pipeline.py` and
   `static_pipeline.py`'s STAC acquisition paths, and `gee_client.py`'s GEE paths, can
   actually execute. Without it I cannot confirm end-to-end behavior for: RT-1's exact
   failure mode on an empty/invalid AOI (N-4 above only traces the code up to the boundary
   with farmdar); the real per-model memory-expansion factor against the `static_classify`
   `model_memory_expansion=12.0` constant (FAIL-7/RT-4) on a model other than the three
   already measured in `FAILURES.md`; or the tile-download/staging behavior R2-1/R2-2's
   fixes were verified against with live catalogue access.
2. **A real AOI vector file with a complete sidecar set** (a `.shp` + `.shx`/`.dbf`/`.prj`,
   or a `.gpkg`), ideally one *outside* Pakistan's UTM zone 42N (e.g. near 61-64°E or
   74-77°E) to make N-3's acreage-distortion claim a measured number rather than a
   geometric argument, and a deliberately invalid/empty/points-only one to reproduce RT-1
   directly through the STAC path rather than by code inspection alone.
3. **A GEE/GCS service-account key** and **GCS/GEE project access**, to exercise
   `needs_gcs`/`needs_gee_api` paths (`gee_static_mode` variants, GEE NDVI, `gs://` model
   and AOI resolution) beyond what `config.py`'s pure validation logic allows me to check
   offline.
4. **What I was trying to test when I hit each gap:** (1) and (3) block any live run at
   all — I substituted static analysis and offline unit-style probes of `config.py` /
   `resources.py` / `postprocess.py` logic instead (see N-1, N-2, N-5, N-6, which are all
   verified by direct execution of pure-Python logic, not by inspection alone). (2) blocks
   turning N-3 (hardcoded CRS) from an architectural argument into a measured acreage delta,
   and blocks reproducing RT-1's exact STAC-path crash text end-to-end rather than tracing it
   to the farmdar boundary.

*(All three gaps above are now closed — see §5.)*

---

## 5. Live verification with real inputs

**Setup completed.** `farmdar.sentinel` was not in this clone (as expected), but was
found already checked out on this machine at
`/home/jovyan/shared/git/standard-libraries/.worktrees/824850c677f49ef5b23af6040e9d2b165e586996`
— the exact worktree hash `CONTEXT.md` names from the earlier campaigns. Made importable
by adding that path to the venv via a `.pth` file (no edits to farmdar itself, per
`README.md`'s "must never be edited here"). GEE/GCS credentials
(`/home/jovyan/FAO/cane/scripts/gcs_data_downloader_ee_farmdar.json`, service account
`gcs-data-downloader@ee-farmdar.iam.gserviceaccount.com`, project `ee-farmdar`) and the
real AOI (`fao_cane_validation_aoi_1.shp`, a single valid Polygon in EPSG:4326, 1 feature,
~7,805 acres, centred at 73.41°E / 31.62°N — near Faisalabad, Punjab) both resolved and
loaded cleanly. The user confirmed all work here is Pakistan-only, which narrows (but, as
§5.2 shows, does not eliminate) the practical impact of N-3.

Two environment differences from the original campaigns' box, both confirmed live and
worth noting for anyone reproducing this with a plain `pip install -r requirements.txt`:

* **No GDAL Python bindings (`osgeo`).** Every live run logged
  `WARNING raster_io: osgeo.gdal unavailable; falling back to in-memory rasterio.merge.`
  This is the exact fallback path `BOTTLENECKS.md` says "was never exercised" in the
  original campaigns, because their box had `osgeo` installed via conda. `requirements.txt`
  correctly notes GDAL bindings are "optional but recommended... install via conda/mamba
  rather than pip" — this is not a cropstack bug, just a real reproducibility gap between
  a `pip`-only setup (what `requirements.txt` alone gives you) and the tested environment.
  Harmless at this AOI's 1-tile scale; per `BOTTLENECKS.md` §5 it would become "the largest
  single allocation in the run" at district/province scale.
* **`boto3` not installed** (it's commented out in `requirements.txt` as optional): every
  run logged `WARNING farmdar.sentinel: aws-creds: bootstrap failed (No module named
  'boto3'); continuing with default 1h provider`. Farmdar handles this gracefully on its
  own; noted only because it's visible in every log and might read as alarming to an
  operator.

### 5.1 Config-combination checks (`--print-config`, real AOI + key, no network)

All of these matched the audited behavior in §2/§3 exactly — cane/wheat/rice presets
resolve correctly, the pre-2018 Landsat-static guard and pre-Sentinel-2 NDVI guard both
fire as documented when called through `build_pipeline_config` directly — **with one
CLI-specific exception, N-8 below, that only shows up when driven through `run.py`'s
actual command line** rather than by calling `build_pipeline_config` in a script (which is
how every prior campaign, and my own §2/§3 checks, exercised these paths).

### 5.2 Live pipeline runs

| Run | Config | Result | Wall time |
|---|---|---|---|
| **A** | cane 2025, `ndvi_source=stac`, `static_source=stac` (auto) | 203 features, **314.5 acres**, `aoi_acres=7834.7`, `static_retention_pct=16.3`, 0 warnings. Static window selector scored all 4 configured windows at 100.0% coverage and took window 1 (2025-11-10), exactly as designed. | 5.8 min (4.8 min acquisition + inference, 0.5 min static, 1 tile) |
| **A, resumed** | same config, `run_mode=resume` (default), re-invoked after A finished | **Identical output** — same 203 features / 314.5 acres, `ndvi_minutes=0.0` (fully checkpointed). Static stage still re-scored all 4 windows over the network before reusing the cached classification (~30s total) — correct behavior (window scores are re-verified every run by design), just means "resume" isn't free for the static stage even when nothing changed. | 0.5 min |
| **C** | cane 2025, `ndvi_source=stac`, `static_source=gee` (`api_auto`) | 218 features, **352.1 acres**, `static_retention_pct=18.0`, 0 warnings. GEE's own auto-selector picked **two** composite dates (2025-11-15, 2025-11-10) where STAC's priority-window selector picked **one** (2025-11-10) — different selection mechanisms, so this is not a same-date backend-parity test like the original campaigns ran (which held the date fixed and got <1% backend disagreement); the ~12% difference here is consistent with the already-documented date-sensitivity finding (FAIL-3/RT-2), not a new backend-radiometry discrepancy. Earth Engine initialised and the GCS export/download round-trip both worked cleanly. | 6.7 min (5.4 min NDVI + 1.2 min GEE static export/download) |

Both runs exited 0, wrote `result_check.json` with all four fields, and produced
schema-correct GPKG + zipped Shapefile outputs. The "core science path" the original
campaigns found solid is confirmed solid again on a third, independent, real production
AOI.

### 5.3 RT-1 — now reproduced live, not just traced

Three deliberately-bad AOI variants (empty, points-only, self-intersecting bow-tie) were
built from/alongside the real AOI and run through `run.py`'s default (STAC) path,
unmodified:

**Empty AOI** — crashes deep inside farmdar, exact match to the error `RETEST_REPORT.md`
documented:

```
File ".../farmdar/sentinel.py", line 444, in _build_tiles
    ncols = int(np.ceil((maxx - x0) / tile_span))
ValueError: cannot convert float NaN to integer
```

**Points-only AOI (no polygon at all)** — reproduces the worst row of RT-1's table exactly:
`cfg.validate()` passes, run folders are created, models resolve, and real Sentinel-2
acquisition **starts** (`AOI -> 1 tiles @ 10m ... 32 workers`) for an AOI with no polygon
area whatsoever. Killed after 45s per a safety timeout, matching `RETEST_REPORT.md`'s own
"it ran for two minutes before I killed it."

**Self-intersecting bow-tie polygon** — same outcome as points-only: proceeds straight to
real acquisition with no geometry check, timed out after 45s still running.

**RT-1's status is now upgraded from "confirmed still broken by code reading" to
"confirmed still broken by direct reproduction," unchanged otherwise.** The fix location
identified in §2 stands: `aoi_io.resolve_aoi` (the function every code path calls) has no
geometry validation; only `gee_client.split_aoi_into_grid` (the GEE-only path) does.

### 5.4 New findings from live testing

#### N-8 (HIGH, live-confirmed) — `run.py`'s own CLI silently defeats the pre-2016 NDVI auto-downgrade it documents, causing the documented "simplest possible run" to crash on any pre-Sentinel-2 year

**File/line:** `run.py:52-53,73-74`, `config.py:745-752`.

`run.py`'s argument parser gives `--ndvi-source` and `--static-source` a `default="stac"`
(`run.py:52-53`), and `main()` unconditionally includes both in the `kwargs` dict passed to
`build_pipeline_config` (`run.py:73-74`) — regardless of whether the user ever typed the
flag. `config.py`'s auto-downgrade logic exists specifically to spare an operator from
knowing about this:

```python
if int(cfg.year) < cfg.sentinel2_start_year and "ndvi_source" not in overrides:
    cfg.ndvi_source = "gee"   # config.py:745-752
```

But because `run.py` always puts `"ndvi_source"` in `overrides` (as `"stac"`, argparse's
default), this check is always False when driven through the CLI — the one thing this
logic exists to detect (the caller having an opinion about `ndvi_source`) can never be
false through this entry point. **Live-reproduced**, exactly as `HOW_TO_USE.md`'s "simplest
possible run" is documented to be typed, on the real AOI:

```
$ python run.py --crop cane --year 2014 --district faisalabad_aoi1 --aoi fao_cane_validation_aoi_1.shp --key ...
...
  File ".../pipeline.py", line 60, in run_pipeline
    cfg.validate()
  File ".../config.py", line 638, in validate
ValueError: ndvi_source='stac' but Sentinel-2 does not cover 2014 (archive starts 2016).
Use ndvi_source='gee', which falls back to Landsat 8 for pre-Sentinel-2 years.
```

Confirmed side-by-side that calling `build_pipeline_config` directly (as every prior
campaign's harness scripts, `batch.py`, and the notebook all do) without an explicit
`ndvi_source` kwarg resolves to `"gee"` and passes `validate()` normally — so this is
specifically a `run.py`-CLI-shaped bug, invisible to every other calling convention in the
codebase, which is exactly why none of the three prior campaigns (which never drove
`run.py`'s CLI) caught it. `run.py` didn't exist yet at campaign 1/2's time either (added
in commit `6158324`, after `RETEST_2.md` was written).

**Why it matters:** this is the primary documented entry point (`README.md`'s "Quick
start", `HOW_TO_USE.md`'s entire guide) for a non-Python user, and cane/wheat/rice all
support pre-2016 years in principle. An operator following the docs exactly, for any
pre-2016 year, hits a stack trace instead of the "sensible default" the docs promise.

**Suggested fix:** give `--ndvi-source`/`--static-source` `default=None` and only add them
to `kwargs` when not `None` (`run.py` already does exactly this for `--region`/`--out`
right next to them — `if args.region: kwargs["region"] = ...`). The same pattern would fix
it in one line per flag.

#### N-9 (MEDIUM, live-confirmed) — an unknown `region` is not caught by `validate()` or `--print-config`; it survives config-build time and only raises after the NDVI stage has already run

**File/line:** `config.py:556-656` (`validate()` — never calls `resolved_static_windows()`),
`config.py:489-503` (where the actual check lives).

Live, with the real AOI and key, an unknown region for wheat (which *does* define regional
windows, unlike N-5's cane case) passes both `--print-config` and an explicit
`cfg.validate()` call with no error or warning of any kind:

```
$ python run.py --crop wheat --year 2025 --district faisalabad_aoi1 --aoi ... --region balochistan --print-config
==============================================================================
crop/year/district : wheat / 2025 / faisalabad_aoi1
...
==============================================================================
(prints cleanly, no error)
```

```python
cfg.validate()                    # passes silently
cfg.resolved_static_windows()     # ValueError: No static windows defined for region
                                   # 'balochistan' on wheat. Known regions: ['punjab', 'sindh'].
```

`validate()`'s own docstring states its purpose: *"Fails fast on inconsistent settings,
before any expensive acquisition."* An unknown region is exactly the kind of typo that
purpose exists to catch, but the check that actually validates it
(`resolved_static_windows`, `config.py:489-503`) is never called from `validate()` — only
from deep inside `static_pipeline.select_dates_by_priority`, which doesn't run until the
static stage begins, **after** the entire NDVI acquisition stage (minutes to tens of
minutes on a real district) has already completed. An operator who typos `--region
punjab` as `--region punjap` for a real wheat district would burn through the full NDVI
stage before finding out.

**Suggested fix:** call `self.resolved_static_windows()` from inside `validate()` (wrapped
to only check region resolution, not date math, if that's cheaper) whenever
`run_static_model` is on and the crop has regional windows — one extra call, and it moves
the existing, correctly-worded error message from "after NDVI acquisition" to "before
anything is downloaded," matching every other `validate()` check in the same function.

#### N-10 (MEDIUM, live-confirmed) — `scikit-learn` has no upper version bound in `requirements.txt`, and a fresh install already diverges from the version the shipped models were pickled with

**File/line:** `requirements.txt:13`.

```
scikit-learn>=1.3      # required to unpickle the RandomForest
```

A completely clean `pip install -r requirements.txt` today resolves `scikit-learn` to
`1.9.1`. Every single NDVI RF inference in every live run in this report logged:

```
InconsistentVersionWarning: Trying to unpickle estimator RandomForestClassifier from
version 1.6.1 when using version 1.9.1. This might lead to breaking code or invalid
results. Use at your own risk.
```

This is not a hypothetical — it fired on the actual production cane model
(`fao_cane_rf_model.joblib`) downloaded from the real `farmdar_data_catalog` bucket, on a
completely clean install of this repo's own `requirements.txt`, today. Whether the
predictions are actually numerically different, I can't say without a version-pinned
environment to compare against (I did not attempt to install a second, older sklearn
purely to test this, since the task didn't call for auditing model outputs against a
pinned baseline) — but scikit-learn's own maintainers are the ones stating the risk, and
it is the exact class of failure this whole campaign is built around: nothing crashes,
nothing in cropstack's own logging surfaces it (the warning goes to Python's `warnings`
module, not through `logging`, so it wouldn't appear in anything that filters/redirects
by log level), and the pipeline runs to a plausible-looking, schema-correct result.

**Suggested fix:** pin `scikit-learn` (and ideally `xgboost`) to the exact version(s) the
shipped models were serialized with, or re-serialize the models against a current pinned
version and update `requirements.txt` together. At minimum, surface the
`InconsistentVersionWarning` through cropstack's own logger (`warnings.catch_warnings` +
re-emit) so it survives whatever log filtering an operator has configured, instead of
depending on raw stderr being watched.

#### N-3, live-confirmed with real numbers on a real Pakistani AOI

Direct measurement on the user's own AOI, no farmdar or network needed:

```
AOI centroid: 73.413°E, 31.623°N  (Faisalabad, Punjab)
AOI's own natural UTM zone: EPSG:32643 (zone 43N)
area in natural zone 32643:     7,805.32 acres
area in hardcoded EPSG:32642:   7,834.75 acres   <- what postprocess.py/qc.py actually report
relative error:                 +0.377%
```

This matches the live pipeline runs exactly — both A and C reported `aoi_acres=7834.7`,
the EPSG:32642 figure, not the AOI's true 7,805.3 acres. **The user's own validation AOI is
itself in UTM zone 43N, not the hardcoded 42N** — Punjab straddles the 42N/43N boundary
near 72°E, and Faisalabad (73.4°E) sits well inside 43N. Given the user works Pakistan-only,
this softens N-3's "wrong hemisphere" worst case, but the +0.377% systematic inflation on
every acreage figure is now a measured fact about real production AOIs in this project's
actual home province, not a hypothetical about deployment elsewhere. `crop_acres=314.5` in
run A is therefore ~0.4% high for the same reason `aoi_acres` is: every stated acreage
in this pipeline's output is computed in the same wrong-for-this-AOI CRS.

---

## 6. Fixes applied and retested — 2026-09-16, same session

**Scope, per explicit instruction: 2018-2025 only.** Everything below applies uniformly
across years and is unrelated to the pre-2016/Landsat era. **N-8 (`run.py`'s CLI defeating
the pre-2016 `ndvi_source` auto-downgrade) was deliberately left unfixed** — it only
affects 2014-2015 configurations, which the user asked to hold off on entirely pending
separate instructions. Nothing else pre-2016-specific was touched either.

Every fix below was (a) implemented, (b) live-verified against the real AOI/credentials
where the finding was originally live-reproduced, and (c) locked in with a new,
permanent, offline regression test in `tests/` (`test_config_validation.py`,
`test_batch_resources.py` — 22 new checks). **The offline suite went from 81/81 to
103/103 passing**, and every fix's live behavior was independently re-confirmed with a
real pipeline run afterward, not just the offline tests.

| Finding | File(s) changed | What changed | Live re-verification |
|---|---|---|---|
| **RT-1** (AOI geometry validation missing on the default STAC path) | `aoi_io.py` (`_validate_and_clean_geometry`, wired into `resolve_aoi`) | Every AOI is now fully read and checked in `resolve_aoi` (the one function every code path uses): empty AOI rejected, invalid geometry repaired with `make_valid()`, non-polygon geometry dropped, "nothing polygonal left" rejected — the exact rules `gee_client.split_aoi_into_grid` already applied, but only on the GEE path. A repaired AOI is written to the AOI cache and that path is used from then on, so every later `gpd.read_file(cfg.aoi_path)` in the pipeline sees the same cleaned geometry without any other file needing to change. | Re-ran all three original live reproductions through the real `run.py` CLI: empty AOI now raises `ValueError: AOI contains no features` in **0.5s** (was: crash deep inside farmdar's tile-grid math); points-only AOI now raises `ValueError: AOI has no polygon geometry after cleaning` in **0.5s** (was: real Sentinel-2 acquisition started); a self-intersecting bow-tie AOI is now silently repaired and a full `--print-config` succeeds. All three used to reach or start network acquisition; none of them do now. |
| **N-3** (headline acreage computed in a CRS hardcoded to UTM zone 42N) | `postprocess.py` (`_local_area_epsg`), `qc.py` (`_aoi_acres`), `static_classify.py` (`_estimate_aoi_pixels`, de-duplicated to reuse the same logic), `aoi_io.py` (new shared `local_utm_epsg(lon, lat)`) | Every area computation now derives its own UTM zone from the geometry's actual location instead of a single hardcoded EPSG code duplicated across two files. `static_classify.py` already had the correct per-AOI logic inline (for a different purpose); it's now the one shared implementation all three call sites use. | Re-ran the full pipeline (run A) on the real AOI after the fix: `aoi_acres` changed from **7834.7** (the wrong, hardcoded-zone figure) to **7805.3** (the AOI's true area in its own zone, 43N) — exactly matching the independently hand-computed figure from §5.2. `crop_acres` moved from 314.5 to 313.3 for the same reason. |
| **N-9** (unknown `region` not caught until the static stage, after the NDVI stage had already run) | `config.py` (`validate()`, `stac`+`auto` branch) | `validate()` now calls `resolved_static_windows()` itself when `stac_static_mode=="auto"`, so an unknown region is caught at config-build time — before any acquisition — using the exact error message that already existed, just never reached this early. | Live: `region="balochistan"` for wheat now raises at `cfg.validate()` (and therefore before `run_pipeline` does anything) instead of passing `--print-config` silently and only surfacing after NDVI acquisition. Valid regions (`punjab`) and no-region crops (`cane`) still validate cleanly. |
| **N-1** (GEE `api_manual`: setting both `gee_static_single_date` and a full top/bottom pair silently drops the single date) | `config.py` (`validate()`, `gee_static_mode=="api_manual"` branch) | `validate()` now raises when both forms are set at once, instead of only checking that *at least one* is present. | Confirmed the exact contradictory config from the original finding now raises `ValueError: ... got both gee_static_single_date (...) and a top/bottom pair (...)`; either form alone still validates cleanly. |
| **N-2** (`batch.py` sizes worker pools for `len(jobs)` districts running concurrently, but jobs run strictly sequentially) | `batch.py` (`run_batch`) | Calls `resources.plan_resources(district_count=1)` unconditionally instead of `district_count=len(jobs)`, matching what the function's own docstring already says about how jobs execute. | On this box (16 core / 121 GiB), a 10-job batch's per-job worker counts went from `ndvi=2, static=2` (computed for 8 "concurrent" districts) to `ndvi=15, static=15` (correct for one district running alone) — confirmed via `resources.plan_resources`'s own `describe()` output before/after. |
| **N-5** (`region` silently no-op for a crop with no regional windows) | `config.py` (`resolved_static_windows()`) | Logs a warning naming the crop and the fact that `region` has no effect, instead of doing nothing silently. Deliberately a warning, not an error, since a batch's shared overrides routinely set `region` for every crop in the batch regardless of whether that crop uses it. | Live: `region="totally_bogus_region"` on `cane` now logs `region='totally_bogus_region' was set but 'cane' has no region-specific static windows -- it has no effect for this crop.` and still runs. |
| **N-6** (`stac_static_dates` silently ignored in `auto` mode for any crop with priority windows — i.e. every shipped crop) | `config.py` (`validate()`) | Warns when both are set together and the priority-window selector will win, naming the fix (`stac_static_mode='manual'`). | Live: confirmed the warning fires for `cane` + `stac_static_mode="auto"` + `stac_static_dates=[...]`, and does **not** fire when the mode is `manual` (where the field is actually used). |
| **RT-6 / OBS-8** (floor logged as `{:.0f}%`, so 99.9% and 100.5% both render as "100%"; the manual-mode anchor warning fired even for a single date with nothing to anchor) | `static_pipeline.py` (`select_dates_by_priority`, `_acquire_static_from_stac`) | Floor now logs at `.1f` precision. The anchor warning now only fires when there is more than one manual date. | Live: `stac_static_min_coverage_pct=99.9` now logs `floor 99.9%` (was `floor 100%`). A single manual date no longer logs an ANCHOR warning; two manual dates still do, naming the correct anchor. |

**Deliberately left unfixed, with reasons:**

* **N-8** — out of scope per this session's instruction (2014-2017 only, held for later).
* **FAIL-5 / RT-5** (static run folders still accumulate unrelated date products) — left
  as-is. The correctness risk this used to carry is already closed by the FAIL-2 fix
  (vector output rebuilds on raster identity, not on folder presence), so this is now
  pure housekeeping. The only fix I could make with confidence — automatically deleting
  sibling date folders — risks deleting output an operator deliberately kept (e.g. for
  comparing two dates), which is a worse failure mode than a folder that needs manual
  tidying. Left for the user to decide the intended behavior before touching it.
* **FAIL-7 / RT-4** (`static_model_memory_expansion=12.0` is a fixed multiplier, not
  measured per model; real measured expansions ranged 9.2×-15.7× across the three models
  in `FAILURES.md`) — left as-is. Changing this constant without measuring more real
  models risks trading one unmeasured guess for another; the existing behavior is
  conservative in the direction that matters (a too-high multiplier under-uses workers
  rather than risking OOM), so it is safe as shipped, just not tight. Flagged, not
  touched.

**Retest after all fixes: `python tests/run_all.py` → 103 passed, 0 failed** (was 81/0).

**Final full-pipeline confirmation** — a completely fresh run (`run_mode=new` for NDVI,
static *and* vector, so nothing was reused from any earlier run in this session), on the
real AOI, with every fix above in place:

```
Pipeline finished in 5.7 min -> .../faisalabad_aoi1_final_cane_2025.gpkg
Result: aoi_acres=7805.3, feature_count=203, crop_acres=313.3,
        crop_share_of_aoi_pct=4.0, static_retention_pct=16.3, warnings=[]
```

**203 features — identical to every pre-fix run of this exact config** (runs A and the
resume test both also got 203). The classification itself is unchanged, as expected: every
fix in this batch touched validation, resource sizing, log wording, or which CRS area is
computed in — none of them touch pixel classification. What changed is exactly what was
supposed to: `aoi_acres` **7834.7 → 7805.3** and `crop_acres` **314.5 → 313.3**, both now
in the AOI's own UTM zone (43N) instead of the hardcoded 42N. Exit 0, zero warnings, GPKG
and zipped Shapefile both written correctly.

---

## 7. Follow-up: N-1 extended into a full disambiguation, `gee_static_manual_layering`

A follow-up question asked exactly the right thing about N-1's fix: what happens if only
`gee_static_single_date` + `gee_static_top_date` are set (no `gee_static_bottom_date`) —
intending top/bottom mode but missing one date? Tracing it: the N-1 fix as first shipped
only caught the *complete-pair* contradiction (all three set). This partial case slipped
through `validate()` (since `has_single` alone satisfied "at least one form is present"),
and only failed later inside `gee_client.build_static_composite` — after the *other* one
of the two dates would have had to be resolved as well, and (worse) after the NDVI stage
had already run, since the static stage always runs after NDVI in `run_pipeline`. It fails
loudly rather than silently, but it wastes a real NDVI acquisition to find out.

**Fix, per the follow-up instruction:** rather than special-casing every combination
separately, `PipelineConfig` now has one explicit toggle,
**`gee_static_manual_layering: Optional[Literal["single", "mosaic"]] = None`**, and one
shared decision function, **`resolved_gee_manual_layering()`**, that both `validate()` and
the code that actually builds the composite (`static_pipeline._acquire_static_from_gee`)
call — so the two can never disagree, and the check always runs before acquisition
(`validate()` is the first line of `run_pipeline`).

**Behavior, all 12 combinations, live-confirmed and locked into
`tests/test_config_validation.py` (N-1.1 - N-1.12):**

| Fields set | Toggle | Result |
|---|---|---|
| `single_date` only | — | `'single'` (no toggle needed) |
| `top_date` + `bottom_date` | — | `'mosaic'` (no toggle needed) |
| all three | none | **raises** — "got both ... set `gee_static_manual_layering` to say which one wins" |
| all three | `'single'` | `'single'` — the pair is ignored |
| all three | `'mosaic'` | `'mosaic'` — the single date is ignored |
| `single_date` + `top_date` only (no bottom) | none | **raises** — "is set together with only one of top_date/bottom_date; drop the stray date, or set the toggle to `'mosaic'` and supply both" |
| `single_date` + `top_date` only (no bottom) | `'mosaic'` | **raises** — "requires both top_date and bottom_date" (still checks both, exactly as asked) |
| `single_date` + `top_date` only (no bottom) | `'single'` | `'single'` — the stray `top_date` is ignored |
| `top_date` only (no bottom, no single) | none | **raises** — "only one of top_date/bottom_date is set" |
| any dates | an unknown toggle string | **raises** — "must be 'single' or 'mosaic'" |
| nothing | none | **raises** — the original "requires single_date, or both top_date and bottom_date" |

Live-confirmed twice through the real `run.py` CLI: `single_date` + `top_date` (no
bottom, no toggle) now fails at `cfg.validate()` in **0.5 seconds**, before the NDVI stage
starts, with the message above naming the exact fix; the same fields with
`--set gee_static_manual_layering=single` build a valid config and proceed normally.

**Files changed:** `config.py` (new field, new `resolved_gee_manual_layering()`,
`validate()` now calls it instead of the narrower N-1 check),
`static_pipeline.py` (`_acquire_static_from_gee` now calls the same function instead of
re-deriving `composite_type` from `cfg.gee_static_top_date` alone).
**Retest:** `python tests/run_all.py` → **112 passed, 0 failed** (was 103; +9 checks for
this).

---

## 8. Crop-switch verification — wheat, a second real AOI

The user supplied a second real AOI to check that switching crops (cane → wheat) doesn't
break anything: `/home/jovyan/FAO/wheat/3/3.shp` — a valid single-polygon AOI near
Sheikhupura/Nankana Sahib, Punjab (74.04°E, 31.46°N), ~25,240 acres, also in UTM zone 43N.
**A pre-existing `wheat_2025/` folder from an older, differently-structured run already
sat next to this AOI; every run below used a separate `--out` directory under this
session's own sandbox and never touched it.**

| Run | Config | Result | Time |
|---|---|---|---|
| **E** | wheat 2025, `region=punjab`, STAC+STAC (auto) | 728 features, **13,240.8 acres**, `static_retention_pct=40.5`, 0 warnings. Static date: 2025-02-15 (window 1, as documented for Punjab). | 6.4 min |
| **E, resumed** | same config, re-invoked | Identical output, `ndvi_minutes=0.0` — full checkpoint reuse. | 0.5 min |
| **F** | wheat 2025, `region=punjab`, STAC NDVI + **GEE** static | 628 features, **14,506.0 acres**, `static_retention_pct=44.4`, 0 warnings. GEE auto-picked two dates (2025-02-18, 2025-02-15) vs STAC's one — same already-documented date-sensitivity pattern as the cane backend comparison in §5.2, not a new issue. | 9.2 min |

**`aoi_acres=25240.0` on both runs — exactly the AOI's true area in its own UTM zone
(43N), confirmed by an independent hand calculation** (25,240.02 acres; the old hardcoded
zone-42N figure would have been 25,378.25 — a 0.548% error, larger than the cane AOI's
0.377% because this AOI sits closer to the zone boundary). The log line itself confirms
the fix picked the right zone: `4. Computing area (EPSG:32643) and filtering...`.

**Nothing crop-specific broke:** wheat's own NDVI/static models resolved and cached
correctly (`FAO_Wheat_RF_Model.joblib`, `FAO_Wheat_XGB_Model.json`), its own class labels
(`ndvi_crop_classes=[14]`, `output_polygon_label=14`) and Punjab priority windows
(confirmed distinct from Sindh's: `[('2025-02-10','2025-02-25'), ...]` vs
`[('2025-02-01','2025-02-20'), ...]`) all applied correctly, and the RT-1 geometry check
passed this AOI through unchanged (it was already valid). `python tests/run_all.py` still
**112 passed, 0 failed** after these runs.

---

## 9. N-3 explicitly reverted by operator decision — back to hardcoded EPSG:32642

After reviewing exactly what the per-AOI dynamic UTM zone fix (§6, N-3) does and does not
change -- the tradeoff being a more accurate acreage per AOI versus every AOI in the
country being measured against the same fixed reference -- **the operator asked for
hardcoded zone 42N (EPSG:32642) back, explicitly declining the per-AOI dynamic zone.**

**Reverted:** `postprocess.py` and `qc.py` are back to the original hardcoded
`AREA_CRS_EPSG = 32642` for every area computation (`crop_acres`, `area_acres`,
`aoi_acres`) — confirmed byte-identical to the pre-audit original for `qc.py`, and
functionally identical (comment-only diff) for `postprocess.py`. Re-verified live:
`qc._aoi_acres()` on the Faisalabad cane AOI now reports **7,834.7495 acres** again (the
original zone-42N figure), not the 7,805.3 zone-43N figure from §6.

**Deliberately left dynamic, by the operator's own choice (offered and declined the
alternative):** `static_classify._estimate_aoi_pixels` — the AOI-pixel-count estimate
behind the degenerate-crop-mask guard — still picks its UTM zone per-AOI via
`aoi_io.local_utm_epsg`. This is not part of the N-3 fix; it has picked a per-AOI zone
this way since the very first commit in this repository's history, before this audit
touched anything. The operator was asked explicitly whether to also hardcode this one to
42N and chose to leave it as originally written.

**Net effect on the codebase:** `aoi_io.local_utm_epsg()` (the shared helper) and the
RT-1 geometry-validation fix in `aoi_io.resolve_aoi` are both retained — neither was in
scope for this reversal. Only the two call sites this audit itself added (`postprocess.py`,
`qc.py`) were rolled back. `python tests/run_all.py` → **112 passed, 0 failed**,
unaffected either way, since no offline test asserted on `postprocess.py`/`qc.py`'s
specific EPSG code.
