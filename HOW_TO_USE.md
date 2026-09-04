# How to use this pipeline

A guide for someone using this code for the first time. Plain English, in order.

---

## 1. What this thing does

You give it:

- a **crop** (cane, wheat, spr_maize, or rice)
- a **year**
- an **AOI** — a shapefile or GeoPackage of the district you want mapped

It gives you back a **map of where that crop is growing**, as polygons, plus the total
acreage.

It does this in three stages:

1. **NDVI stage** — downloads a whole season of Sentinel-2 satellite images, watches how
   green each pixel gets over time, and uses a Random Forest model to guess which pixels
   look like that crop's growth pattern.
2. **Static stage** — downloads **one** clear image from the crop's best time of year and
   uses an XGBoost model to double-check the pixels the first stage flagged. This removes
   most false positives.
3. **Vector stage** — turns the surviving pixels into polygons, clips them to your AOI,
   drops anything smaller than half an acre, and writes a GeoPackage and a zipped
   Shapefile.

You do not need to run the stages yourself. One command does all three.

---

## 2. Before you start

You need three things:

| Thing | What it is |
|---|---|
| **AOI file** | Your district boundary. `.shp`, `.gpkg`, `.geojson`, `.parquet`, `.kml`, `.fgb`, or a zipped shapefile — all work. |
| **Service-account key** | A Google JSON key file. Needed to download the models, and for the GEE backend if you use it. |
| **Python environment** | The one with the pipeline's dependencies installed. |

**About shapefiles:** a `.shp` is not one file. It needs its `.shx`, `.dbf` and `.prj`
next to it. If you copy only the `.shp` the pipeline will stop and tell you so. Copy the
whole set, or use a `.gpkg` instead — one file, no sidecars, less to go wrong.

---

## 3. The simplest possible run

```bash
python run.py \
    --crop wheat \
    --year 2025 \
    --district kasur \
    --aoi /path/to/Kasur.shp \
    --key /path/to/service-account.json
```

That is it. Everything else has a sensible default.

When it finishes you get, under the output folder:

```
3_vector_run_1/final_output/kasur_wheat_2025.gpkg    <- the polygons
3_vector_run_1/final_output/kasur_wheat_2025.zip     <- same thing as a Shapefile
3_vector_run_1/result_check.json                     <- the numbers, see section 7
```

**Check the config before committing to a long run:**

```bash
python run.py ... --print-config
```

This prints what the pipeline decided — dates, models, windows — and stops without
downloading anything. Cheap, and worth doing the first time you set up a new district.

---

## 4. How long it takes, and how much space

Measured on a real district (Kasur, 3,984 km², 8 CPU cores):

| | |
|---|---|
| Satellite download | ~43 minutes (57 tiles) |
| Everything after it | ~7 minutes |
| Disk used at peak | ~9 GB |
| RAM used at peak | ~9 GB |

Download time depends on your internet, not on this code. A small test AOI takes a few
minutes end to end.

**Before starting a district, check you have disk space.** A rough rule: about
**150 MB per tile**, and a district is tens of tiles. Kasur needed 9 GB. The raw tiles
are deleted automatically once they have been used.

---

## 5. Useful options

### Wheat needs a region

Wheat is sown and harvested at different times in Punjab and Sindh, so the pipeline
needs to know which:

```bash
python run.py --crop wheat --year 2025 --district kasur --region punjab ...
```

Cane, spring maize and rice do not need this.

### Resume after a crash

```bash
python run.py ... --run-mode resume     # this is the default
```

Resume is **on by default** and it is safe. It reuses whatever finished, and redoes what
did not. A half-downloaded tile is never mistaken for a finished one. If your session
dies, just run the same command again — do not start over.

Use `--run-mode new` only when you deliberately want a fresh run folder alongside the old
one.

### Change any setting

```bash
python run.py ... --set static_chunk_size=1024
python run.py ... --set stac_static_mode=manual --set 'stac_static_dates=["2025-02-15"]'
```

`--set` reaches any configuration field. Values are read as JSON where possible, so
numbers, `true`/`false`, `null` and lists all work. Anything you set this way wins over
what the pipeline would have chosen.

### Many districts at once

Put the jobs in a JSON file (see `jobs.example.json`) and run:

```bash
python batch.py --jobs jobs.json --results results.csv
```

One district failing does not stop the rest. You get a CSV row per district saying
whether it worked and how long it took.

---

## 6. It sizes itself to your machine

You do **not** need to tune worker counts. On startup the pipeline looks at your CPU
cores, your free RAM and how many districts you are running, and decides for itself. It
prints what it decided:

```
Resource plan: 8 core(s), 61.0 GiB free -> 4 district(s) at a time (12.2 GiB each);
per district: ndvi=2, static=2, stac=4, chunk=2048
```

On a small laptop it runs districts one at a time and uses smaller chunks. On a big
server it runs many at once. If you want to control it yourself, `--set` still wins, or
use `--no-auto-resources` to turn the sizing off entirely.

If you ever hit an out-of-memory error, the first thing to try is:

```bash
python run.py ... --set static_chunk_size=1024
```

That quarters the memory each worker needs and costs very little speed.

---

## 7. Reading the result

Every run writes `result_check.json` next to the polygons:

```json
{
  "aoi_acres": 984439.6,
  "crop_acres": 288123.3,
  "crop_share_of_aoi_pct": 29.3,
  "feature_count": 30546,
  "static_retention_pct": 31.4,
  "warnings": []
}
```

- **`crop_acres`** — the headline number, how many acres of the crop were found.
- **`crop_share_of_aoi_pct`** — that as a percentage of the district. Sanity-check this
  against what you know of the area.
- **`static_retention_pct`** — how much of the NDVI stage's guess the static model kept.
  **This is reported, not judged.** The pipeline will not tell you whether it is good,
  because that depends on the crop and the district and it genuinely does not know. Real
  runs so far have landed between 20% and 44%.
- **`warnings`** — usually empty. It only warns about two things: the static model kept
  **nothing** (0%) or kept **everything** (100%). Both mean something went wrong rather
  than that the crop is absent.

**A district with genuinely no crop is a valid answer.** You will get a GeoPackage with
the right columns and zero rows, and the run exits normally. That is different from a
failure, and the pipeline keeps the two apart on purpose.

---

## 8. Things the log tells you — worth reading

**Which satellite date it chose, and what else it considered:**

```
Window scores (4 of 4 window(s) scored):
  window 1 (2025-02-10 to 2025-02-25): ['2025-02-15', '2025-02-10'] -> 99.1% of AOI usable
  window 2 (2025-01-25 to 2025-02-10): ['2025-02-05'] -> 100.0% of AOI usable
  ...
Coverage spread across the scored windows: 99.1%-100.0%.
```

Each crop has several preferred date ranges. The pipeline scores them all and picks the
earliest good one, because earlier windows are better dates agronomically.

**Check the "N of M" count.** If it says `3 of 4 window(s) scored`, one window could not
be checked — usually a temporary problem with the satellite catalogue. If the window it
could not check ranked higher than the one it chose, **the run stops on purpose** rather
than give you a number from a worse date while looking confident. Wait a bit and run
again.

**The date matters a lot.** On one test AOI the same crop, same year, same everything
except the date gave answers between 931 and 8,322 acres. The pipeline picks a
defensible date and shows you the alternatives; it cannot tell you which is truly right.
If a result looks wrong against what you know locally, try the next window:

```bash
python run.py ... --set static_window_start_at=2
```

**Download speed:**

```
STAC acquisition took 42.7 min for 57 tile(s) -- 5.7 min/tile
```

Around 2–6 minutes per tile is normal. If you see a warning that tiles are averaging far
more than that, your credentials have probably expired. Refresh them and re-run with
resume — do not sit and wait it out.

**Errors that mean something specific:**

| Message | What it means |
|---|---|
| `nodata in every pixel` | No satellite imagery exists for that year/area. This is a download failure, **not** a district with no crop. Check the year and the AOI. |
| `No usable static imagery in any configured window` | Every candidate date was unusable — usually cloud. |
| `could not be scored ... rank higher` | A better date could not be checked. Retry. |
| `is missing its .shx sidecar` | You copied only the `.shp`. Copy the whole set. |

---

## 9. If something goes wrong

1. **Read the last error line.** They are written to be read, and most of them tell you
   what to do next.
2. **Run the same command again.** Resume is the default and it is safe. Most
   interruptions cost you nothing but the time already spent.
3. **Out of memory:** `--set static_chunk_size=1024`.
4. **Out of disk:** old run folders are safe to delete once you have copied the
   `final_output` folder out.
5. **Result looks wrong:** check `result_check.json` and the window scores in the log
   first. The date is usually the reason.

---

## 10. Where things live

| Path | What it is |
|---|---|
| `run.py` | Run one district from the command line. Start here. |
| `batch.py` | Run many districts. |
| `fao_crop_mapping.ipynb` | The same thing as a notebook, if you prefer that. |
| `config.py` | Every setting and its default, with the reasoning in comments. |
| `README.md` | Deeper technical detail. |
| `tests/run_all.py` | The test suite. Run it if you change anything. |
| `CONTEXT.md` | Working notes on what is in progress. Not a user guide. |

---

## 11. Two honest limitations

1. **There is no ground truth in this pipeline.** It cannot tell you its answer is
   correct — only what it found and how it decided. Check the acreage against what you
   know of the district before passing it on.
2. **The date drives the number.** Two defensible dates can give very different
   acreages. The pipeline shows you every date it considered so the choice is visible
   rather than hidden. If a number surprises you, look at the window scores first.
