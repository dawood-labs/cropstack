# BOTTLENECKS.md — where the time and memory actually go

All numbers are measured, not estimated, unless a line says "extrapolated". Raw
per-0.5-second samples are in `metrics/*_samples.csv`; stage boundaries in
`metrics/*_events.json`; timeline plots in `metrics/*_timeline.png`.

Box: 8 vCPU (4 physical cores, HT), 61 GiB RAM, NVMe. `osgeo.gdal` 3.13.2 is present,
so mosaics take the streaming VRT path — the in-memory `rasterio.merge` fallback was
never exercised.

## The one-line answer

**Imagery acquisition is 89% of wall clock. Everything this repository optimised — the
streaming mosaic, the 8-bytes/pixel sieve, worker recycling, the dissolve removal —
together accounts for about 8 seconds of a 546-second run.** Tuning the compute stages
cannot make a material difference to runtime; only fetching less data can.

**Memory is a separate story with a separate answer.** Acquisition dominates the peak on
cane, wheat and rice — but the single largest peak in the whole campaign is
`static_classify` on spr_maize at **27.89 GiB**, and it has nothing to do with imagery:
it is one 5.2 GiB XGBoost model loaded once per worker, with the pool sized from CPU
count (§4b, `FAILURES.md` FAIL-7). Fetching less data would not move it at all.

Full baseline run, `A1_cane_2025` (cane, 2025, STAC NDVI + STAC static, 546 s):

| stage | wall | share | mean CPU (800% = all cores) | peak tree RSS |
|---|---|---|---|---|
| `resolve_models` (first run only) | 15 s | 3% | 9% | 0.26 GiB |
| **`ndvi_acquire_stac`** | **417 s** | **76%** | **189%** | **5.45 GiB** |
| RF inference (4 tiles, 6 workers) | 38 s | 7% | up to 788% | 5.72 GiB |
| `ndvi_mosaic` (GDAL VRT → GTiff) | <1 s | ~0% | — | — |
| sieve (NDVI) | 1 s | ~0% | — | — |
| **`static_acquire_stac`** | **70 s** | **13%** | **30%** | 3.36 GiB |
| `static_crop_mask` | <1 s | ~0% | — | — |
| `static_classify` (7 workers) | 5 s | 1% | 294% | 5.44 GiB |
| sieve (static) | <1 s | ~0% | — | — |
| `vector_stage` | 1 s | ~0% | — | — |

Acquisition = 487 s of 546 s.

---

## 1. NDVI acquisition is network-bound, and tiling adds ~22% overhead

**Evidence.** `ndvi_acquire_stac` is 417 s of a 546 s run with tree CPU averaging 189%
of the 800% available — 24% of the box. It is waiting on the network, not computing.
One cane run pulls **5.04 GB** against a final output of 5.5 MB on disk.

The AOI splits into 4 tiles at the default `stac_tile_size_deg=0.1`, and all 4 fall
inside a single Sentinel-2 granule (T43RCQ). The log shows the same asset being seeded
into the cache layer once per tile:

```
s2-cache MISS seeded S2B_MSIL2A_20251016T054729_R048_T43RCQ/B05 (72188642 bytes)
   ... the same line four times, once per tile
```

That looks like a 4× duplicate download, and I initially reported it as one. It is not —
see the correction below — but the tiles do overlap enough to cost measurable extra
time and bytes.

**Fix — measured, not guessed.** I tested the obvious lever, `stac_tile_size_deg`,
which collapses this AOI's 4 tiles into 1 (cane 2025, NDVI-only, everything else held):

| config | tiles | acquisition | network in | peak RSS | features | acres |
|---|---|---|---|---|---|---|
| `tile_deg=0.1` (default) | 4 | 419 s | 2,109 MB | 5.77 GiB | 1,309 | 3,134.6 |
| `tile_deg=0.2` | 1 | **326 s** | **1,637 MB** | **6.84 GiB** | 1,309 | 3,134.7 |
| `stac_worker_count=2` | 4 | 477 s | 1,975 MB | **5.17 GiB** | 1,309 | 3,134.6 |

**22% faster, 22% less network, 19% more memory, byte-identical product.**

The third row is the opposite lever, also measured: cutting `stac_worker_count` from 8
to 2 costs **14% wall clock** (419 s → 477 s) and buys **10% peak RSS** (5.77 → 5.17 GiB),
with the product again identical. Note the acquisition is already worker-capped at 4 by
tile count here, so 8→2 is really 4→2; on a district AOI with ≥8 tiles the memory saving
would be much larger and the time penalty similar. That is the knob to reach for on a
memory-constrained box.

> **Correction to an earlier draft.** I first estimated "up to ~4× less network" here, on
> the reasoning that each tile re-downloads the whole shared scene asset. That was wrong.
> The `s2-cache MISS seeded … (72188642 bytes)` lines report the *asset* size being
> seeded into the cache layer, not bytes transferred — odc-stac reads only each tile's
> window out of the COG via HTTP range requests, so the four tiles were already fetching
> largely disjoint data. 22% is the real overlap-and-per-request overhead.

So the two levers worth pulling, in order:

1. **Configure the read-through cache.** `farmdar.sentinel` supports
   `FARMDAR_S2_CACHE_BUCKET` (`boto3` is commented out in `requirements.txt`). This is
   the one that matters across *runs* rather than within one: every scenario in this
   campaign re-fetched the same imagery, and the resume tests showed re-acquisition
   dropping from 70 s to 18 s once the OS page cache was warm. A shared bucket cache
   would make repeat district runs — the actual production pattern — near-free.
2. **`stac_tile_size_deg=0.2`** for a measured 22% off both wall clock and network, at
   19% more RAM. **Pair it with a lower `stac_worker_count`** on district-sized AOIs
   (see §4) — on an AOI large enough to still yield ≥8 tiles, memory grows roughly
   linearly in tile area.

## 2. STAC static acquisition is also network-bound — and GEE shows how cheap it could be

`static_acquire_stac` is 70–101 s and 93% of the static stage, downloading 2.5–3.0 GB
for one cloud-free composite. The GEE path fetches the *same product* as a single
server-side composite:

| static backend | wall | network received | peak RSS |
|---|---|---|---|
| STAC (`A1`, 1 date) | 70 s | ~2.5 GB | 3.36 GiB |
| STAC (`A2`, 2 dates) | 101 s | 3.0 GB | 1.18 GiB |
| GEE (`A3`/`A4`) | 114 s / 110 s | **40 MB** | **0.90 GiB** |

**~60× less network and ~4× less memory.** The catch is latency, not throughput — see
§3.

The backends themselves *are* equivalent: given the same date they agree to 0.5%
(`TEST_REPORT.md` §3). What is not equivalent is the **spelling of a two-date config** —
STAC takes the anchor from `stac_static_dates[0]` while GEE names `gee_static_top_date`
explicitly, so the same pair of dates written the natural way puts a different image on
top (`FAILURES.md` FAIL-3). Settle that convention before treating GEE as a drop-in
replacement; the imagery is not the obstacle.

## 3. GEE static spends 95% of its stage idle-polling the export queue

**Evidence.** In `A3_cane_2025`, 223 of 245 samples (93.9%) during `static_acquire_gee`
show tree CPU < 30% *and* < 1 MB/s network — 112 s of a 118 s stage doing nothing but
`ee.data.getTaskStatus` every 20 s (`gee_client.wait_for_export_tasks`). The actual
download is 36.6 MB in ~1 s.

The 20 s poll interval is reasonable; the latency is GEE's batch queue. **Fix:** submit
the static export *before* the NDVI stage rather than after it. The static composite
depends only on the AOI bounding box, not on the NDVI result — `_acquire_static_from_gee`
builds its geometry straight from the shapefile — so the export can be in flight during
the 417 s NDVI stage and be ready when the static stage begins.
*Expected gain: ~110 s per run, i.e. the whole GEE static stage becomes free.*

### 3b. The same trade, 10× larger, on the GEE **NDVI** backend

`G_gee_ndvi_cane_2025` runs the NDVI stage through GEE instead of STAC (same AOI, crop,
year and models; only `ndvi_source` differs):

| NDVI backend | acquisition | network in | peak RSS | mean CPU during stage |
|---|---|---|---|---|
| STAC (`W4`) | 419 s | 2,109 MB | 5.77 GiB | 189% |
| GEE (`G`) | **1,233 s** | **495 MB** | **4.02 GiB** | **1%** |

Over 2,459 samples of `ndvi_acquire_gee`: mean tree CPU **1%**, median 0%, **99.8% of
samples below 30% CPU**, peak RSS 336 MB. The client holds no imagery and does no work —
it blocks on the export queue for 20 minutes. This is §3's pattern at ten times the
duration.

**The trade, stated plainly: GEE NDVI costs 2.9× the wall clock to save 4.3× the network
and 1.75 GiB of peak RAM.** Neither backend is more accurate — the products agree to
0.2% (`TEST_REPORT.md`, parity). On a network- or memory-constrained box GEE is the right
choice; on this one STAC is three times faster. The §3 fix (submit exports early) applies
here with far more upside.

## 4. Peak memory tracks concurrency × tile area, not AOI size

**Evidence.** `A1` peaks at 5.72 GiB tree / 5.58 GiB in a single process, entirely
inside `ndvi_acquire_stac`, with 4 tiles in flight → **~1.46 GiB per in-flight tile**.
`farmdar.sentinel` fetches `min(stac_worker_count, n_tiles)` tiles concurrently, so a
bigger AOI does not raise the peak — but the defaults do:

| `stac_worker_count` | `tile_deg` | extrapolated peak |
|---|---|---|
| 8 (default) | 0.1 (default) | 11.4 GiB |
| 8 | 0.2 | **45.8 GiB** (of 61 GiB) |
| 4 | 0.2 | 22.9 GiB |

This box only stayed at 5.7 GiB because the AOI has 4 tiles, fewer than the 8 workers.
**A district-sized AOI at the shipped defaults will run 8 tiles concurrently and use
~11 GiB** — survivable here, not on a 16 GiB box. Combining the §1 fix (bigger tiles)
with the default 8 workers would OOM.

*Recommendation: treat `stac_worker_count × tile_deg²` as the memory budget. If
`tile_deg` goes to 0.2, drop `stac_worker_count` to 3–4.*

### 4b. …except in `static_classify`, where it tracks workers × **model size**

The largest peak measured anywhere in this campaign is not acquisition at all:

| scenario | `static_classify` peak | mask px kept | static model on disk |
|---|---|---|---|
| `A1_wheat_2025` | 4.67 GiB | 1,407,169 | 0.7 MB |
| `A1_cane_2025` | 5.44 GiB | 153,043 | 27.5 MB |
| `A1_spr_maize_2025` | **27.89 GiB** | 439,255 | **563.2 MB** |

It does not track pixels — wheat masks 3.2× more pixels for one sixth of the memory. Each
spawn worker loads its own copy of the model, and the spr_maize XGBoost expands **9.2×**
from its 563 MB JSON to **5.2 GiB resident** (measured per-process:
`harness/static_model_memory_probe.py`). `worker_count` defaults to `cpu_count() - 1`, so
the model term alone is `7 × 5.365 GiB = 37.6 GiB` — and it scales with the *machine*,
not the work: 31 workers on a 32-core box would need 161 GiB of identical models.

*Recommendation: size the static pool as `min(cpu_count()-1, n_windows, budget //
model_rss)`, measuring `model_rss` once in the parent. Until then, set
`static_worker_count=2–3` for any crop whose static model is over ~100 MB.* Full write-up
in `FAILURES.md` FAIL-7.

## 5. What is NOT a bottleneck (measured, so it can stop being optimised)

* **The sieve and vectorise steps.** Marginal cost measured at **8.21 bytes/pixel** by
  tiling the real raster to 5.0 / 44.6 / 123.9 / 242.8 Mpx in fresh processes —
  confirming `postprocess.py`'s own "roughly 8 bytes/pixel" claim. In AOI terms at 10 m:

  | AOI | pixels | sieve+vectorise peak |
  |---|---|---|
  | this test AOI | 4 Mpx | 0.03 GB |
  | Okara district (4,377 km²) | 44 Mpx | 0.36 GB |
  | Rahimyar Khan (11,880 km²) | 119 Mpx | 0.98 GB |
  | Punjab province (205,344 km²) | 2,053 Mpx | 16.9 GB |

  Comfortable at district scale; only a concern at province scale. Vectorising 242.8 Mpx
  took 11.5 s.
* **The mosaic.** GDAL VRT → GTiff, under 0.5 s, no measurable memory. (The in-memory
  `rasterio.merge` fallback was never triggered because `osgeo` is installed; on a box
  without GDAL bindings this would become the largest single allocation in the run.)
* **RF inference.** 38 s for 4 tiles — 7% of the run.
* **Model download.** 15 s once, then permanently cached (`cache hit` on every later
  run).

## 6. Pre-2018 years cost ~18x the network for a worse product

Same AOI, same bands, same tile size, same 8-day step, comparable number of composite
windows (~41 vs ~42), measured over the `ndvi_acquire_stac` stage alone:

| year | acquisition | network in | throughput |
|---|---|---|---|
| 2025 (`A1_cane_2025`) | 401 s | 2,268 MB | 5.7 MB/s |
| 2016 (`A1_cane_2016`) | **953 s** | **39,822 MB** | 41.8 MB/s |

**17.5x the bytes, 2.4x the time.** Whole-run totals: 40,274 MB against 5,043 MB. No
retries appear in the 2016 log (one incidental "error" string; no HTTP/429/503/timeout
lines), so this is not error amplification. The likely cause is that 2016 Sentinel-2
scenes on the Planetary Computer are older reprocessings with partial footprints and
less range-request-friendly assets, so each compositing window pulls far more bytes.

*Caveat: network is sampled system-wide via `psutil.net_io_counters()` on a shared node.
The 41.8 MB/s is sustained for the full 16-minute stage and stops when the stage does, so
attribution is near-certain but not process-isolated the way RSS and disk IO are.*

**Operationally: budget ~40 GB and ~17 min per district-equivalent AOI for a pre-2018
STAC year, against ~5 GB and ~9 min for a recent one.** And note the product is worse,
not merely dearer — see `FAILURES.md` FAIL-6 (the 2016 static image is 83% cloud) and
FAIL-8 (the Landsat alternative returns 9.93 acres).

## 7. Smaller inefficiencies

* **6 NDVI workers are spawned for 4 tiles.** `ndvi_worker_count` defaults to 75% of
  cores (6); parallelism is capped by tile count. Two processes start, load nothing and
  idle. Harmless here, but it means the default is sized to the machine rather than to
  the work. **The static stage has the identical flaw and there it is not harmless** —
  7 workers for 4 windows, each holding a full model copy (§4b).
* **Empty `raw_ndvi_tiles/` and `tile_predictions/` directories reappear on every
  resumed run**, because `run_ndvi_pipeline` mkdirs them before testing the checkpoint.
  Cosmetic.
* **Disk is well managed.** `delete_raw_ndvi_tiles` reclaims essentially everything:
  `A1_cane_2025` peaked at 646 MB and left 5.5 MB (99%); `B_rice_2025` 536 MB → 3.8 MB.
  Peak disk is ~120× the final output, so transient space, not steady state, is what
  needs provisioning.
