# CONTEXT.md — session memory

Ye status file hai, diary nahi. Session ke shuru mein SABSE PEHLE ye parho.

---

## 1. Abhi ka maqsad

Do kaam:
1. **Retention warning nikalna** — `qc_min/max_static_retention_pct` fitted thresholds
   thay (15 / 99.5), koi agronomic base nahi. Dono `None` karne hain. Number report
   karna hai, faisla nahi sunana. Sirf degenerate cases (0% / 100%) par warning.
2. **7dd73a5 ke R2-1..R2-5 fixes ko asli data pe verify karna** — abhi tak sirf mock
   tests se verify hue hain.

Ownership: main (Claude) hi fix karta hoon, test likhta hoon, commit karta hoon.
Domain faisla (kaunsi acreage sahi, kaunsi date behtar, kaunsa threshold) — user se
poochna hai, khud nahi karna.

---

## 2. Jo ho chuka hai

- `git pull` → main `7dd73a5` par hai.
- **Purani reports commit ho gayi** (`b5ddd94`): TEST_REPORT / FAILURES / BOTTLENECKS /
  RETEST_REPORT / RETEST_2 + `metrics/ harness/ specs_retest2/ logs/`. 333 files.
- **CONTEXT.md** bana (`0d9e0c1`).
- **Task 1 mukammal**: retention bounds `None`; retention hamesha report hota hai;
  sirf degenerate (0% / 100%) warnings; README theek; `tests/` suite bani — 27 checks.
- `harness/unit_tests.py` ka parquet test theek kiya (test ka apna bug tha,
  `resolve_aoi` parquet ko gpkg banata hai) — ab 15/15.
- **R2-1 verify (7/7 PASS)** — `harness/verify_r2_1_score_failure.py`,
  `logs/verify_r2_1.log`, `metrics/verify_r2_1.json`. Window 1 ki scoring fail karayi
  (RateLimited patch), windows 2/3 asli catalogue se.
- **R2-2 verify (8/8 PASS)** — `harness/verify_r2_2_staging.py`, `logs/verify_r2_2.log`,
  `metrics/verify_r2_2.json`. Purane tiles discard hote hain, same-date resume reuse
  karta hai (manual aur auto dono).
- **R2-3 verify (7/7 PASS)** — `harness/verify_r2_3_empty_vs_failure.py`. 2027 (spr_maize
  aur rice dono) ruk jata hai; asli khaali sub-AOI ko empty GPKG + exit 0 milta hai.
- **R2-4** — chaar crops ka retention, bounds `None`, zero warnings:
  cane 36.9 · wheat/punjab 30.2 · wheat/sindh 25.9 · spr_maize 20.5.
- **R2-5** — asli run: 3.3 min / 4 tiles → **2.6 min/tile**, basis = farmdar ki apni
  per-tile durations. Purana wall/tiles form 0.8 kehta. 8 min threshold fire nahi hua.
- **Regression PASS** — fresh full run (`W5_cane_fresh_1016`, run_mode=new):
  926 features / **2,184.67 acres** → delta **0.0%**. NDVI map byte-identical.
- `ndvi_pipeline.per_tile_minutes()` alag function bana (testable). Tests: 41.

---

## 3. Abhi kis cheez par kaam chal raha hai — bilkul is waqt

**PRODUCTION DISTRICT RUN ki tayyari.**
AOI: `~/FAO/wheat/kasur_testing/Kasur.shp` — Kasur, **3,984 km2 / 984,440 acres**,
bbox par 88 tiles @ 0.1 deg. Okara test AOI (418 km2, 4 tiles) se **~9.5x bara**.
Crop: wheat, region punjab, year 2025. STAC dono stages.

**Disk probe HO GAYA (naapa, guess nahi):** ek raw NDVI tile = **144 MiB**
(wheat series 2024-08-24 -> 2025-07-01, step 8). 88 tiles → **12.4 GiB** peak.
Baqi (static staging ~1.2 GiB, mosaics/sieved ~0.5 GiB, vectors) ke saath total
**~15 GiB**. Free 35 GB → theek hai.

**RUN SHURU KAR DIYA:** `D1_kasur_wheat_2025`
spec `specs_retest2/D1_kasur_wheat_2025.json`, output `runs_district/D1_kasur_wheat_2025`,
log `logs/D1_kasur_wheat_2025.log`, `run_mode=new`.
Asli tile count **57** (88 sirf bbox tha) → peak raw NDVI **8.2 GiB**.
**NDVI acquisition MUKAMMAL (09:36):** `42.7 min for 57 tile(s) -- 5.7 min/tile
(mean of farmdar's per-tile durations)`. Warning nahi aayi (5.7 < 8.0).
**AHEM:** asli district par healthy = **5.7 min/tile**, Okara ke 2.6 se do guna.
8 min threshold ab sirf **1.4x** door hai — pehle main ne 3.1x bataya tha, wo
chhote AOI ka number tha. Ye user ko batana hai.
**RF inference HANG HO GAYA (09:37:54 se koi harkat nahi).** Ye asli production bug hai.

**FAIL-13 (HIGH) — NDVI inference pool `max_tasks_per_child` recycle par deadlock:**
- 6 workers (75% of 8 cores) x `ndvi_worker_max_tasks=8` = **48**. Theek 48/57 tiles
  ke baad ruka. 49th task ke liye naye workers spawn hone thay — wahin phansa.
- Koi worker process zinda nahi; parent 25 threads, CPU 0.5%, `futex_wait_queue`,
  ek thread `do_poll` par (mare hue workers ka pipe).
- `ndvi_tile_timeout_s=900` **bekaar hai**: wo `future.result(timeout=)` par lagta hai
  jo `as_completed()` ke *baad* chalta hai — us waqt future pehle hi complete hota hai.
  `as_completed()` khud **be-timeout** hai. Isliye hang forever.
- Chhote AOI par kabhi nahi dikha kyunki Okara ke 4 tiles < 48 thay.
- **15 line mein reproduce kiya** (CPython 3.11.15 ka bug, humara nahi).
- **FIX HO GAYA:** `ndvi_pipeline.run_tile_inference()` — `max_tasks_per_child` ki
  jagah har batch ke liye naya pool (memory bound wahi rehta hai), aur timeout
  `as_completed()` par (jo asal mein block karta hai). Stall par workers kill karke
  saaf RuntimeError, jo resume karne ko kehta hai.
- Fix likhte waqt ek aur bug: timeout handler ke andar `Path(tile).name` fail hua to
  cleanup skip ho gaya aur hang wapas aa gaya. Ab cleanup pehle hota hai.
- Tests: `tests/test_pool_recycle.py` — wahi 6x8=48 shape. **51 checks** total.

**DISTRICT RUN RESUME KAR RAHA HOON (10:25):** spec ab `run_mode="resume"`.
48 tile_predictions + 57 raw tiles disk par mehfooz hain, is liye sirf 9 tiles
baqi hain. Uske baad mosaic → sieve → static stage → vector.
Agar phir mari: wahi resume dobara chalao.
**Agar instance yahan mari:** log parho, `run_mode="resume"` se dobara chalao —
NDVI tiles resume ho jayenge.

---

## 4. Agla qadam

1. Task 2 baqi: R2-3 (2027 vs Z1), R2-4 (chaar crops ka retention),
   R2-5 (min/tile basis + 8 min threshold).
2. Regression: pinned-date 2,184.7 acres → delta 0.0% hona chahiye.
3. `specs/` + `specs_retest/` — 269 checks har commit se pehle.

---

## 5. Ahem numbers aur faisle

- **Regression baseline:** cane 2025, static date pinned `2025-10-16` →
  **926 features / 2,184.7 acres**. Delta 0.0% expected.
- **spr_maize window spread** (same AOI, same NDVI, sirf date badli):
  win1 (05-09+05-01) = 3,992.8 ac · win2 (04-29) = 8,322.0 ac · win3 (05-14) = 931.7 ac.
  8.9x spread. Kaunsa sahi hai — **maloom nahi, ground truth nahi hai.**
- **Asli retention** (chaar crops, ek AOI): 20.5–43.5%. **Ye band koi rule nahi hai** —
  6 observations hain. Isi se 15% banaya gaya tha, wo galat tha.
- **Environment:**
  - farmdar worktree: `.worktrees/824850c677f49ef5b23af6040e9d2b165e586996`
    (purana `30c67408…` gayab hai, shallow clone). `harness/run.sh` purane path par hai —
    **`harness/run2.sh` use karo.**
  - `.gitignore` mein blanket `*.json` credential rule hai → evidence JSON ke liye
    `git add -f`, pehle key scan karke.
  - NDVI stage muft resume hota hai: naye output dir mein `1_ndvi_run_1/` copy karo,
    phir `run_mode="resume"`. NDVI product bit-identical rehta hai.

---

## 6. Jo maine mana kiya / jispe razi nahi hua

- **Push ab CHALTA HAI.** Remote SSH par hai (`git@github.com:dawood-labs/cropstack.git`),
  key `~/.ssh/cropstack`, `~/.ssh/config` mein set. **Har commit ke baad push karo.**
  Backup: `~/cropstack_backup.bundle` (`git bundle create ... --all`).
  Purana masla: HTTPS par koi credentials nahi thay.
- **Dhyan:** pichli dafa main ghalti se `test-campaign/retest-2` branch par kaam karta
  raha aur samajhta raha ke `main` par hoon (`git checkout main && git merge` wala
  compound command block hua tha, akela `merge` ne "Already up to date" kaha). Ab main
  fast-forward ho chuki hai. **Har commit se pehle `git branch --show-current` dekho.**
- Threshold khud nahi banana. 15% wali galti dobara nahi.
- `qc_degenerate_retention_tolerance_pct = 0.0` — **user ne 0 par razi hoke faisla diya.**
  **Natija jo user ko maloom hai aur qabool hai:** 0.3% retention wala collapse ab
  **warning NAHI dega** — sirf bilkul 0.0% dega. Number phir bhi report hota hai.
  **Agar kabhi field mein aisa near-collapse case dikhe (retention 0% aur ~2% ke beech,
  jo galat nikle) to user ko YAAD DILANA — wo isay dobara dekhna chahte hain.**
- **"269 checks" wali test suite repo mein maujood nahi thi.** 7dd73a5 ke commit message
  mein likha hai magar wo files kabhi commit nahi hui. Ab `tests/` bani hai — 41 checks
  + `harness/unit_tests.py` ke 15.
