# What was actually wrong, and what actually changed

No vague summaries here — every item below states the exact setting/field involved,
exactly what the code did before, exactly what it does now, and the real numbers from
the actual tests run. File names refer to files inside the `cropstack` repo.

---

## Bug 1: A broken or empty AOI boundary file was never checked

**Where:** `aoi_io.py`, function `resolve_aoi()`.

**Exactly what the old code checked:** does the file exist on disk, are the `.shx`/`.dbf`
shapefile sidecar files present, and can `geopandas` read row 1 without erroring. That's
it. It never opened the whole file to check whether the shape(s) inside were valid, were
actually polygons, or whether there even were any rows at all.

**Exactly what went wrong, with real examples I ran:**
- An AOI file with **zero rows** (completely empty): the old code let it straight
  through. It crashed later, but not with a useful message — the error came from deep
  inside the satellite-download library: `ValueError: cannot convert float NaN to
  integer`, because an empty AOI has no bounding box, so its coordinates come out as
  "not a number."
- An AOI file with **only two point markers, no polygon at all**: the old code let this
  through completely. I ran it for real: it built the run folders, loaded the crop
  model, and then printed `AOI -> 1 tiles @ 10m ... 32 workers` and **started downloading
  real Sentinel-2 satellite tiles** for an area that has no actual boundary — for a
  boundary that literally cannot enclose any land.
- An AOI with a **self-crossing polygon** (a "bow-tie" shape, where the boundary line
  crosses itself): same thing — accepted with no complaint, and this specific shape is
  known to make the map-clipping step later produce wrong results.

**Exactly what changed:** `resolve_aoi()` now opens the *entire* AOI file (not just row
1) and does, in order:
1. If it has 0 rows → raises `ValueError: AOI contains no features` immediately.
2. Checks every row's shape with Shapely's `.is_valid`. Any invalid one gets repaired
   automatically with `.make_valid()` (this is the exact same repair method the code
   already used elsewhere, on a different code path, for the Google Earth Engine side —
   it's now applied here too).
3. Drops any row that isn't a `Polygon` or `MultiPolygon` (so stray points or lines get
   removed).
4. If nothing polygon-shaped is left after that → raises `ValueError: AOI has no polygon
   geometry after cleaning`.
5. If anything was actually repaired/dropped, it saves a cleaned copy of the file and
   uses that from then on; if the file was already fine, it's used unchanged (no
   unnecessary rewriting).

**Confirmed by actually running it:**
- Empty AOI → now fails in **0.5 seconds** with `ValueError: AOI contains no features`,
  before any model is loaded or any folder is created.
- Two-points-only AOI → now fails in **0.5 seconds** with `ValueError: AOI has no
  polygon geometry after cleaning`.
- Bow-tie AOI → gets silently repaired; `--print-config` then completes normally,
  showing the AOI path pointed at the repaired copy.

---

## Bug 2: Setting a Google Earth Engine manual satellite date could pick the wrong one, silently

**Where:** `config.py` (the `PipelineConfig` settings) and `static_pipeline.py`.

**The three relevant settings:**
- `gee_static_single_date` — "use this one date."
- `gee_static_top_date` + `gee_static_bottom_date` — "layer these two dates, with
  `top_date`'s image drawn on top of `bottom_date`'s image."

**Exactly what the old code did:**
```python
composite_type = "mosaic" if cfg.gee_static_top_date else "single"
```
This line only asked one question: "is `gee_static_top_date` filled in?" It never
checked whether `gee_static_single_date` was *also* filled in. So:
- If you set `gee_static_single_date="2025-11-10"` and, from an earlier test, also still
  had `gee_static_top_date="2025-11-10"` and `gee_static_bottom_date="2025-10-16"` left
  set — the code would silently go into "mosaic" (two-date) mode, throw away
  `gee_static_single_date` entirely, and use the old two-date pair instead. Nothing was
  printed to say this happened.

**Exactly what changed:** added a new setting, `gee_static_manual_layering`, which can be
`"single"`, `"mosaic"`, or left unset (`None`). Added one function,
`resolved_gee_manual_layering()`, that both the validation step and the actual
image-building step now call — so they can never disagree with each other. It checks
every real combination:

| What's filled in | `gee_static_manual_layering` | What happens now |
|---|---|---|
| only `single_date` | not set | uses single — unchanged |
| only `top_date` + `bottom_date` | not set | uses mosaic — unchanged |
| all three | not set | **now raises:** `"got both gee_static_single_date ('2025-11-10') and a top/bottom pair ('2025-11-10', '2025-10-16'). Set gee_static_manual_layering='single' or 'mosaic'..."` |
| all three | `"single"` | uses single, pair is ignored (your explicit choice) |
| all three | `"mosaic"` | uses mosaic, single_date is ignored (your explicit choice) |
| `single_date` + `top_date` only (`bottom_date` missing) | not set | **now raises immediately:** `"...is set together with only one of gee_static_top_date / gee_static_bottom_date... Drop the stray date, or set gee_static_manual_layering='mosaic' and supply both."` |
| same as above | `"mosaic"` | **still raises** — `"requires both gee_static_top_date and gee_static_bottom_date"` (it checks the actual dates are there, the setting alone doesn't bypass that) |
| same as above | `"single"` | uses single, the stray `top_date` is ignored |

**Why this second row of cases (`single_date` + only one of the pair) matters
concretely:** before this was found and fixed, that exact case would pass validation,
run the entire NDVI download stage (several minutes), and only fail once it got to
actually building the satellite image — wasting all that download time to discover one
missing date. Live-tested: it now fails in the same 0.5 seconds as the other config
errors, before the NDVI stage starts.

All 12 of the rows above (plus a couple more edge cases like an unrecognised value for
`gee_static_manual_layering`) are locked into `tests/test_config_validation.py`, and all
12 were also individually run through the real `config.py` code (not just the tests) to
confirm the actual behavior matches what's written above.

---

## Bug 3: A misspelled wheat `region` wasn't caught until after the NDVI download finished

**Where:** `config.py`, function `validate()`.

**The setting:** `region="punjab"` or `region="sindh"` — wheat's two supported regions,
each with its own best satellite-date windows.

**Exactly what the old code did:** `validate()` — the function that's supposed to catch
config mistakes "before any expensive acquisition" (its own docstring says this) — never
called the function that actually checks the region name (`resolved_static_windows()`).
That function was only called once the static-image stage started, which is the *second*
of three stages, after the NDVI stage (which downloads a whole season of satellite
imagery and can take several minutes to tens of minutes) had already finished.

**Real example I ran:** `region="balochistan"` (wheat has no data for Balochistan — its
two real regions are Punjab and Sindh). With the old code: `--print-config` printed the
config with no complaint at all; `cfg.validate()` also raised nothing. The error only
appeared when I manually called `cfg.resolved_static_windows()`:
`ValueError: No static windows defined for region 'balochistan' on wheat. Known regions:
['punjab', 'sindh'].` — but in a real run, this only happens after NDVI has already run.

**Exactly what changed:** `validate()` now calls `resolved_static_windows()` itself
whenever `stac_static_mode == "auto"` (the default). Re-ran the exact same
`region="balochistan"` config: `cfg.validate()` now raises the same error message
immediately — **before** `run_pipeline()` does anything at all, confirmed by checking
the Python traceback, which shows the error coming from inside `validate()`, called at
the very first line of `run_pipeline()`, before any model download or satellite call.

---

## Bug 4: Batch runs (many districts, one file) were given far less computing power than they should have had

**Where:** `batch.py`, function `run_batch()`.

**Exactly what the old code did:**
```python
plan = resources.plan_resources(district_count=len(jobs))
```
`plan_resources` decides how many parallel "workers" (separate processes doing the heavy
computation) each district gets, based on how many districts it's told will be running
*at the same time*. `run_batch()`'s own loop, though, runs jobs one after another — I
checked the loop directly: it's a plain `for` loop with no threading or multiprocessing
across jobs, so only one district is ever actually being processed at any moment. But
by passing `district_count=len(jobs)`, the old code was telling `plan_resources` "assume
`len(jobs)` districts running simultaneously," which made it divide up the machine's
cores in advance for a concurrency that never actually happens.

**Real, measured numbers, on the actual machine this was tested on (16 CPU cores, 121 GB
free RAM):**
- Old behaviour, batch of 10 districts: `plan_resources(district_count=10)` returned
  `ndvi_worker_count=2, static_worker_count=2` for **every one** of the 10 districts —
  even though they run one at a time.
- New behaviour, same batch of 10 (or any number) of districts:
  `plan_resources(district_count=1)` returns `ndvi_worker_count=15,
  static_worker_count=15` — the number a single district actually gets to use when
  nothing else is competing for the machine, which is the true situation for every
  district in the batch.

**Exactly what changed:** the line above now reads `plan = resources.plan_resources(
district_count=1)` — a constant `1`, not `len(jobs)` — because that's what's actually
true: batch jobs never overlap.

---

## Bug 5 & 6: Two settings that quietly did nothing, with zero feedback

**Bug 5 — `region` on a crop that doesn't use regions.** Only wheat has region-specific
date windows (`static_priority_windows_by_region` is only filled in for wheat in
`config.py`). If you set `region="anything"` for `cane`, `spr_maize`, `rice`, or
`cotton`, the code that reads this (`resolved_static_windows()`) silently used the
crop's normal windows and never looked at `region` at all — no error, no message, even
for a nonsense value like `region="totally_bogus_region"`.
**Change:** the same function now prints a warning:
`region='totally_bogus_region' was set but 'cane' has no region-specific static windows
-- it has no effect for this crop.` — it still runs normally (this is a legitimate
situation, since a batch file often sets the same `region` for every crop in it
regardless of whether each one uses it), but now you're told.

**Bug 6 — `stac_static_dates` set while `stac_static_mode="auto"`.** For every crop
that actually has a static model configured (cane, wheat, spr_maize),
`stac_static_mode="auto"` uses that crop's own priority date-windows and never looks at
`stac_static_dates` at all — that setting only does anything in `stac_static_mode=
"manual"`. Setting both together (e.g. `stac_static_mode="auto"` +
`stac_static_dates=["2025-01-01"]`) used to be silently accepted with the date simply
ignored.
**Change:** `validate()` now prints: `stac_static_dates=['2025-01-01'] was set but
stac_static_mode='auto' and 'cane' has priority windows configured -- the priority-window
selector decides the date(s) instead, and stac_static_dates has no effect. Set
stac_static_mode='manual' to use it.`

---

## Bug 7: Two small log-message mistakes

**Where:** `static_pipeline.py`, function `select_dates_by_priority` and
`_acquire_static_from_stac`.

**7a — rounding.** The coverage floor was printed using Python's `{floor:.0f}` format
(rounds to a whole number). Concretely: setting `stac_static_min_coverage_pct=99.9`
printed `(floor 100%)` in the log — identical to what `stac_static_min_coverage_pct=
100.5` would also print, even though 99.9 and 100.5 behave completely differently (one
is almost always satisfied, the other almost never is). **Change:** now uses `{floor:
.1f}`, one decimal place, so it correctly prints `floor 99.9%` versus `floor 100.5%`.
Confirmed live: re-ran with `stac_static_min_coverage_pct=99.9` and the log now reads
exactly `Chose window 1 (2025-11-07 to 2025-11-15) at 100.0% (floor 99.9%).`

**7b — anchor warning on a single date.** In manual static mode, the code printed:
`Manual static dates: 2025-11-10 is the ANCHOR (layered on top and used as the
radiometric reference)...` — this warning is about which of *two* dates gets layered on
top of the other. It used to print even when there was only **one** manual date, where
there's nothing being layered and the word "anchor" doesn't mean anything.
**Change:** this now only prints when `len(selected_dates) > 1`. Confirmed live with two
test runs on the same real AOI: one manual date → no anchor warning printed; two manual
dates → the exact same warning as before, correctly, since there really is a top/bottom
relationship there.

---

## Bug 8: Acreage was measured using one fixed map zone for the whole country — fixed, then reverted on request

**Where:** `postprocess.py` and `qc.py`, constant `AREA_CRS_EPSG = 32642`.

**What `32642` concretely is:** the code for "UTM Zone 42 North" — a map projection
that's exactly accurate along the line at 69°E longitude, and gets progressively less
accurate the further east or west of that line you measure.

**The real numbers, from the actual AOI files supplied for this testing:**
- The cane field near Faisalabad sits at 73.41°E. That is **not** in zone 42N — it's in
  "UTM Zone 43 North" (`EPSG:32643`, accurate along 75°E). Measuring it with zone 42N's
  math instead of zone 43N's gave **7,834.7495 acres**. Measuring it correctly (zone
  43N) gives **7,805.3162 acres**. That's **29.43 acres too many, +0.377%**, from using
  the wrong zone.
- The wheat field near Sheikhupura sits at 74.04°E — also zone 43N, and further from
  zone 42N's accurate line than the cane field is. Old (wrong-zone) measurement:
  **25,378.2466 acres**. Correct measurement: **25,240.0209 acres**. That's **138.2
  acres too many, +0.548%**.

**What was changed (first pass):** added a function (`local_utm_epsg` in `aoi_io.py`)
that works out the correct UTM zone from a location's actual longitude and latitude
(`zone = int((lon + 180) / 6) + 1`, then picks the Northern or Southern-hemisphere EPSG
code), and wired `postprocess.py` and `qc.py` to use it instead of the fixed `32642`.
Confirmed live: `qc._aoi_acres()` on the cane field returned exactly **7,805.3162** —
matching the independently-calculated correct number.

**Then explicitly reverted, on request:** `postprocess.py` and `qc.py` were changed back
to always use `AREA_CRS_EPSG = 32642`, no matter where the AOI is. Confirmed live after
reverting: `qc._aoi_acres()` on the same cane field now returns **7,834.7495** again —
the original number, back to how it was before this testing started.

**One thing that was deliberately *not* reverted, by explicit choice:**
`static_classify.py`'s `_estimate_aoi_pixels()` function (used only for an internal
sanity check on whether the crop mask looks degenerate — not for any acreage number an
operator actually sees) already calculated its own correct UTM zone before this testing
ever began, on the very first commit of this codebase. That one was left as it always
was; the question was asked explicitly and the answer was to leave it alone.

---

## Things found, and left alone on purpose — with the specific reason for each

**1. `run.py`'s command-line flags default to the wrong satellite source for years
before 2016.** `run.py` defines `--ndvi-source` and `--static-source` with
`default="stac"` in its `argparse` setup, and always passes that default value into the
config, even if you never typed either flag. Separately, `config.py` has logic that's
supposed to automatically switch to `ndvi_source="gee"` for years before 2016 — but only
when the caller never mentioned `ndvi_source` at all. Because `run.py` always mentions
it (as "stac", the default), that automatic switch never triggers when run from the
command line. Real result: `python run.py --crop cane --year 2014 ...` (with no
`--ndvi-source` flag typed) crashes with `ValueError: ndvi_source='stac' but Sentinel-2
does not cover 2014 (archive starts 2016)...`. Calling the same config directly from
Python (skipping `run.py`'s command line) does **not** have this problem — the automatic
switch works correctly there. **Left unfixed, explicitly, per instruction to hold off on
2014-2017-related changes.**

**2. Old date-folders pile up in a static-run folder when you change the date.** If you
run a static-image stage, then run it again for the same district with a different
date, both the old date's output folder and the new date's output folder are kept side
by side inside the same run folder (e.g. `18_Oct_2025_and_10_Nov_2025/` sitting next to
`16_Oct_2025/`). This used to be a real risk — the tool could hand back results from the
wrong date. That specific risk is already fixed elsewhere (the tool now checks which
raster file actually produced a given output before reusing it). What's left is just
that the old folder isn't deleted. **Left alone** because automatically deleting a
folder risks deleting output someone deliberately kept for comparison — a worse outcome
than a slightly untidy folder.

**3. `static_model_memory_expansion = 12.0`.** When sizing how many parallel workers can
safely run a large static-image model, the code estimates each worker's memory use as
"the model's file size × 12." Measured on three real models from the earlier test
campaigns: wheat's model expanded **15.7×** (0.7 MB file → 11 MB resident), cane's
expanded **10.4×** (27.5 MB → 285 MB), and spring maize's expanded **9.2×** (563 MB →
5,165 MB). So `12.0` is a reasonable middle value but not measured for every model.
**Left as-is** rather than swap one unmeasured guess for another.

---

## A new problem, found this session, written down, deliberately not fixed yet

**Exact trigger:** running any crop for year 2016 or 2017 with `ndvi_source="gee"`
(which uses Landsat 8 imagery, 30-metre pixels) together with `static_source="stac"`
(which uses Sentinel-2 imagery, 10-metre pixels) — a combination only possible in those
two years.

**What happened, with real numbers:** running spr_maize, year 2016, AOI "6", with that
exact combination, the tool's own result check reported:
`static_retention_pct = 277.9%` and a warning: `"The static model kept 277.9% of the
NDVI stage's crop area -- effectively all of it..."`. A percentage over 100% is
logically impossible for what this number means (the static stage can only ever confirm
or reject pixels the NDVI stage already flagged — it can never end up with *more*).

**The exact cause, found by checking the raw numbers:** the NDVI stage (Landsat, 30m
pixels) reported `ndvi_crop_pixels = 1497`. The static stage (Sentinel-2, 10m pixels)
reported `static_crop_pixels = 4160`. `qc.py`'s calculation is simply
`100 * static_crop_pixels / ndvi_crop_pixels` — dividing one pixel count by the other,
with no idea that a Landsat pixel covers **9 times** the ground area of a Sentinel-2
pixel (30m × 30m = 900 sq m, versus 10m × 10m = 100 sq m). Converting both counts into
real ground area instead of raw pixel count: `1497 × 900 sq m = 332.9 acres` (NDVI) versus
`4160 × 100 sq m = 102.8 acres` (static) — giving a sensible **30.9%**, not 277.9%.

**Status:** written down here and in `TEST_REPORT_2026-09-16.md`, and will be
remembered. **Not fixed** — testing of years 2014-2017 for this session was done with
`run_static_model=False` (static stage turned off entirely), which sidesteps this
specific bug for now since it only happens when the static stage actually runs.

---

## Testing done to confirm all of the above

- The automatic offline test suite (`python tests/run_all.py`) went from **81 passing
  checks** at the start of this work to **112 passing checks** at the end — the 31 new
  checks cover every fix listed above, and every single one currently passes (`112
  passed, 0 failed`).
- Beyond the offline checks, real end-to-end pipeline runs were executed against three
  real boundary files supplied for this testing:
  - **Cane**, `fao_cane_validation_aoi_1.shp` (near Faisalabad, 73.41°E) — run with STAC
    imagery for both stages, then again with Google Earth Engine for the static stage,
    then again after every fix, each time producing 203 matching crop-area features.
  - **Wheat**, `3.shp` (near Sheikhupura, 74.04°E, region=punjab) — run with STAC for
    both stages (728 features, 13,240.8 acres) and again with Google Earth Engine for
    the static stage (628 features, 14,506.0 acres).
  - **Spring maize**, `fao_spr_maize_validation_aoi_6.shp` — run for years 2014, 2015,
    2016 and 2017, with Landsat imagery (`ndvi_source="gee"`) and the static stage
    turned off, exactly matching how this crop is actually used for those years; all
    four years completed with no errors and no warnings.
