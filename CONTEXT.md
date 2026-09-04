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

Task 2 **HO GAYA** — R2-1..R2-5 aur regression sab asli data par verify.
Ab: aakhri khulasa likhna. Koi pending code kaam nahi.

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

- **`git push` BLOCKED hai** permission classifier se. Commits chal rahe hain, push nahi.
  Sab kuch local commit ho raha hai. User ko push khud karna hoga ya permission deni
  hogi. **Ye har session mein dobara check karo.**
- Threshold khud nahi banana. 15% wali galti dobara nahi.
