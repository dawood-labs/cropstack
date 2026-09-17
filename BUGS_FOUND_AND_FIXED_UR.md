# Asal mein kya ghalat tha, aur asal mein kya badla

Ye vague nahi hai — har item mein exact setting/field ka naam, code pehle exactly kya
karta tha, ab exactly kya karta hai, aur asal tests ke real numbers diye hain. File names
`cropstack` repo ke andar ki files hain.

---

## Bug 1: Kharab ya khaali AOI boundary file kabhi check hi nahi hoti thi

**Kahan:** `aoi_io.py`, function `resolve_aoi()`.

**Purana code exactly kya check karta tha:** kya file disk par maujood hai, kya `.shx`/
`.dbf` shapefile sidecar files maujood hain, aur kya `geopandas` row 1 bina error ke
parh sakta hai. Bas itna hi. Kabhi poori file khol kar ye check nahi karta tha ke andar
ki shape(s) valid hain ya nahi, wo asal mein polygons hain ya nahi, ya file mein koi row
hai bhi ya nahi.

**Exactly kya ghalat hua, real examples ke sath jo maine khud chalaye:**
- Ek AOI file **zero rows** ke sath (bilkul khaali): purana code isay seedha guzar dene
  deta tha. Baad mein crash hota, lekin faidamand paighaam ke sath nahi — error satellite-
  download library ke andar se aata: `ValueError: cannot convert float NaN to integer`,
  kyunke khaali AOI ka koi bounding box nahi hota, is liye uske coordinates "not a
  number" ban jaate hain.
- Ek AOI file jis mein **sirf do point markers, koi polygon hi nahi**: purana code isay
  poori tarah guzar dene deta tha. Maine ye asal mein chalaya: run folders bane, crop
  model load hua, phir `AOI -> 1 tiles @ 10m ... 32 workers` print hua aur **asal
  Sentinel-2 satellite tiles download hona shuru ho gaye** — ek aisi area ke liye jiska
  koi asal boundary hi nahi hai.
- Ek AOI jismein **self-crossing polygon** ho (ek "bow-tie" shape, jahan boundary ki
  lakeer khud se katti hai): wahi baat — bina kisi shikayat ke qabool ho jata, aur ye
  specific shape aage clipping step mein ghalat results deta hai.

**Exactly kya badla:** `resolve_aoi()` ab AOI file ki **poori** file kholti hai (sirf row
1 nahi) aur is tarteeb mein karti hai:
1. Agar 0 rows hain → foran `ValueError: AOI contains no features` deta hai.
2. Har row ki shape ko Shapely ke `.is_valid` se check karta hai. Koi bhi invalid mile to
   `.make_valid()` se khud-ba-khud theek kar deta hai (yehi exact repair method jo code
   mein pehle se, kisi doosri jagah, Google Earth Engine wale hisse mein use hoti thi —
   ab yahan bhi lagayi gayi hai).
3. Koi bhi row jo `Polygon` ya `MultiPolygon` nahi hai, usay hata deta hai (chhitre hue
   points ya lines waghera).
4. Agar iske baad koi polygon-shaped kuch bacha na ho → `ValueError: AOI has no polygon
   geometry after cleaning` deta hai.
5. Agar kuch repair/hata hua ho, to file ki ek saaf copy save karke ab se wahi use
   karta hai; agar file pehle se theek thi, waisi hi rakhi jaati hai (bila wajah dobara
   likhna nahi).

**Khud chala kar confirm kiya:**
- Khaali AOI → ab **0.5 second** mein fail hoti hai, `ValueError: AOI contains no
  features` ke sath, kisi model load ya folder banane se pehle.
- Sirf-do-points wali AOI → ab **0.5 second** mein fail hoti hai, `ValueError: AOI has no
  polygon geometry after cleaning` ke sath.
- Bow-tie AOI → khud-ba-khud repair ho jaati hai; `--print-config` phir normal tareeqe se
  chal jata hai, AOI path repaired copy ki taraf point karta hua.

---

## Bug 2: Google Earth Engine manual satellite date set karna kabhi khamoshi se ghalat date choose kar leta tha

**Kahan:** `config.py` (`PipelineConfig` ki settings) aur `static_pipeline.py`.

**Teen related settings:**
- `gee_static_single_date` — "yehi ek date use karo."
- `gee_static_top_date` + `gee_static_bottom_date` — "in do dates ko layer karo,
  `top_date` ki tasveer `bottom_date` ki tasveer ke upar lagayi jayegi."

**Purana code exactly kya karta tha:**
```python
composite_type = "mosaic" if cfg.gee_static_top_date else "single"
```
Ye line sirf ek sawal poochti thi: "kya `gee_static_top_date` bhara hua hai?" Ye kabhi
check nahi karti thi ke `gee_static_single_date` **bhi** bhara hai ya nahi. Is liye:
- Agar aap `gee_static_single_date="2025-11-10"` set karte, aur pichle kisi test se
  `gee_static_top_date="2025-11-10"` aur `gee_static_bottom_date="2025-10-16"` bhi set
  reh gaye hote — code khamoshi se "mosaic" (do-date) mode mein chala jata,
  `gee_static_single_date` ko poori tarah phenk deta, aur purana do-date joda use karta.
  Kuch bhi print nahi hota ye batane ke liye ke ye hua hai.

**Exactly kya badla:** ek nayi setting add ki, `gee_static_manual_layering`, jo
`"single"`, `"mosaic"`, ya khaali (`None`) ho sakti hai. Ek function
`resolved_gee_manual_layering()` banaya jise ab validation step **aur** asal
image-banane wala step dono call karte hain — is liye ye dono kabhi aapas mein disagree
nahi kar sakte. Ye har asal combination check karta hai:

| Kya bhara hai | `gee_static_manual_layering` | Ab kya hota hai |
|---|---|---|
| sirf `single_date` | set nahi | single use hota hai — waisa hi |
| sirf `top_date` + `bottom_date` | set nahi | mosaic use hota hai — waisa hi |
| teeno | set nahi | **ab error deta hai:** `"got both gee_static_single_date ('2025-11-10') and a top/bottom pair ('2025-11-10', '2025-10-16'). Set gee_static_manual_layering='single' or 'mosaic'..."` |
| teeno | `"single"` | single use hota, joda ignore (aapka apna faisla) |
| teeno | `"mosaic"` | mosaic use hota, single_date ignore (aapka apna faisla) |
| `single_date` + sirf `top_date` (`bottom_date` nahi) | set nahi | **ab foran error deta hai:** `"...is set together with only one of gee_static_top_date / gee_static_bottom_date... Drop the stray date, or set gee_static_manual_layering='mosaic' and supply both."` |
| wahi upar wala | `"mosaic"` | **phir bhi error deta hai** — `"requires both gee_static_top_date and gee_static_bottom_date"` (asal dates check karta hai, sirf setting se ye bypass nahi hota) |
| wahi upar wala | `"single"` | single use hota, stray `top_date` ignore |

**Ye doosri wali row (`single_date` + jode mein se sirf ek) kyun ahem hai, concretely:**
ye case mile aur fix hone se pehle, ye exact case validation se guzar jata, poori NDVI
download stage chalata (kai minute), aur **tab** fail hota jab asal satellite image
banane ki koshish karta — poora download waqt zaya karke ek chhoti si missing date
dhoondne ke liye. Live-test kiya: ab ye baaki config errors ki tarah **0.5 second** mein
hi fail ho jata hai, NDVI stage shuru hone se pehle.

Upar ki 12 rows (aur kuch aur edge cases jaise `gee_static_manual_layering` ki ghalat
value) `tests/test_config_validation.py` mein permanently lock ki gayi hain, aur sab 12
ko asal `config.py` code se bhi individually chala kar confirm kiya gaya hai.

---

## Bug 3: Wheat ke `region` ki ghalati tab tak nahi pakdi jaati thi jab tak NDVI download poora nahi ho jata

**Kahan:** `config.py`, function `validate()`.

**Setting:** `region="punjab"` ya `region="sindh"` — wheat ke do supported regions, har
ek ki apni behtareen satellite-date windows.

**Purana code exactly kya karta tha:** `validate()` — jis function ka kaam hai config ki
ghaltiyan "kisi bhi expensive acquisition se pehle" pakadna (uska apna docstring yehi
kehta hai) — kabhi us function ko call nahi karta tha jo asal mein region ka naam check
karta hai (`resolved_static_windows()`). Wo function sirf tab call hota tha jab static-
image stage shuru hoti (jo teen stages mein se **doosri** hai), NDVI stage (jo poore
season ki satellite imagery download karta hai, kai minute se dus minute tak lag sakta
hai) poora hone ke baad.

**Real example jo maine chalaya:** `region="balochistan"` (wheat ke paas Balochistan ka
data nahi hai — uske asal do regions Punjab aur Sindh hain). Purane code ke sath:
`--print-config` config print kar deta bina kisi shikayat ke; `cfg.validate()` bhi kuch
raise nahi karta. Error sirf tab dikhi jab maine khud `cfg.resolved_static_windows()`
call kiya: `ValueError: No static windows defined for region 'balochistan' on wheat.
Known regions: ['punjab', 'sindh'].` — lekin asal run mein ye sirf NDVI chalne ke baad
hota.

**Exactly kya badla:** `validate()` ab khud `resolved_static_windows()` call karta hai
jab kabhi `stac_static_mode == "auto"` (default) ho. Wahi `region="balochistan"` wala
config dobara chalaya: `cfg.validate()` ab wahi error foran raise karta hai — `run_
pipeline()` ke kuch bhi karne se **pehle**, Python ka traceback confirm karta hai ke
error `validate()` ke andar se aa raha hai, jo `run_pipeline()` ki bilkul pehli line par
call hota hai, kisi model download ya satellite call se pehle.

---

## Bug 4: Batch runs (kai districts, ek file) ko unse bohat kam computing power di jaa rahi thi

**Kahan:** `batch.py`, function `run_batch()`.

**Purana code exactly kya karta tha:**
```python
plan = resources.plan_resources(district_count=len(jobs))
```
`plan_resources` faisla karta hai har district ko kitne parallel "workers" (alag
processes jo bhari calculation karte hain) milne chahiye, is hisaab se ke kitne districts
**ek sath** chal rahe honge. `run_batch()` ka apna loop, halanke, jobs ek ke baad ek
chalata hai — maine loop khud check kiya: ye ek plain `for` loop hai, jobs ke darmiyan
koi threading ya multiprocessing nahi, is liye kisi bhi lehze mein sirf ek district
process ho raha hota hai. Lekin `district_count=len(jobs)` pass karke, purana code
`plan_resources` ko batata tha "farz karo `len(jobs)` districts ek sath chal rahe hain,"
jo machine ke cores pehle se aisi concurrency ke liye baant deta jo asal mein kabhi hoti
hi nahi.

**Real, measured numbers, isi machine par jahan test kiya (16 CPU cores, 121 GB free
RAM):**
- Purana behavior, 10 districts ki batch: `plan_resources(district_count=10)` **har
  ek** district ke liye `ndvi_worker_count=2, static_worker_count=2` deta tha — chahe wo
  ek waqt mein ek hi chalte hain.
- Naya behavior, wahi 10 (ya kitne bhi) districts ki batch: `plan_resources(
  district_count=1)` `ndvi_worker_count=15, static_worker_count=15` deta hai — wo number
  jo ek district ko asal mein milna chahiye jab machine par koi aur competition na ho, jo
  har district ke liye batch mein asal haqiqat hai.

**Exactly kya badla:** upar wali line ab `plan = resources.plan_resources(
district_count=1)` hai — ek fixed `1`, `len(jobs)` nahi — kyunke yehi asal mein sach hai:
batch jobs kabhi overlap nahi karte.

---

## Bug 5 aur 6: Do settings jo khamoshi se kuch nahi karti thi, bina kisi feedback ke

**Bug 5 — `region` us crop par jo region use hi nahi karta.** Sirf wheat ke paas region-
specific date windows hain (`config.py` mein `static_priority_windows_by_region` sirf
wheat ke liye bhara hai). Agar aap `cane`, `spr_maize`, `rice`, ya `cotton` ke liye
`region="kuch bhi"` set karein, jo function ise parhta hai (`resolved_static_windows()`)
khamoshi se crop ki normal windows use kar leta aur `region` ko bilkul dekhta hi nahi —
koi error nahi, koi paighaam nahi, chahe value `region="totally_bogus_region"` jaisi
bemaani hi ho.
**Badlaao:** wahi function ab warning print karta hai: `region='totally_bogus_region'
was set but 'cane' has no region-specific static windows -- it has no effect for this
crop.` — chalta hai normal tareeqe se (ye jaiz situation hai, kyunke batch file mein
aksar ek hi `region` har crop ke liye set kiya jata hai chahe har ek use kare ya nahi),
lekin ab aap ko pata chal jata hai.

**Bug 6 — `stac_static_dates` set kiya jab `stac_static_mode="auto"` ho.** Har us crop
ke liye jiska static model asal mein configured hai (cane, wheat, spr_maize),
`stac_static_mode="auto"` us crop ki apni priority date-windows use karta hai aur
`stac_static_dates` ko bilkul dekhta hi nahi — wo setting sirf `stac_static_mode=
"manual"` mein kuch karti hai. Dono ek sath set karna (jaise `stac_static_mode="auto"` +
`stac_static_dates=["2025-01-01"]`) pehle khamoshi se qabool ho jata tha, date bas
ignore ho jaati.
**Badlaao:** `validate()` ab print karta hai: `stac_static_dates=['2025-01-01'] was set
but stac_static_mode='auto' and 'cane' has priority windows configured -- the priority-
window selector decides the date(s) instead, and stac_static_dates has no effect. Set
stac_static_mode='manual' to use it.`

---

## Bug 7: Do chhoti log-message ki ghaltiyan

**Kahan:** `static_pipeline.py`, functions `select_dates_by_priority` aur
`_acquire_static_from_stac`.

**7a — rounding.** Coverage floor Python ke `{floor:.0f}` format (poore number tak
round) se print hota tha. Concretely: `stac_static_min_coverage_pct=99.9` set karne se
log mein `(floor 100%)` print hota — bilkul wahi jo `stac_static_min_coverage_pct=100.5`
bhi print karta, halanke 99.9 aur 100.5 bilkul mukhtalif tareeqe se behave karte hain (ek
taqreeban hamesha poora hota hai, doosra taqreeban kabhi nahi). **Badlaao:** ab `{floor:
.1f}` use hota hai, ek decimal place, is liye ab `floor 99.9%` aur `floor 100.5%` sahi se
alag print hote hain. Live confirm kiya: `stac_static_min_coverage_pct=99.9` ke sath
dobara chalaya, log mein bilkul yehi likha aaya: `Chose window 1 (2025-11-07 to
2025-11-15) at 100.0% (floor 99.9%).`

**7b — ek hi date par anchor warning.** Manual static mode mein, code print karta tha:
`Manual static dates: 2025-11-10 is the ANCHOR (layered on top and used as the
radiometric reference)...` — ye warning is baat ke bare mein hai ke *do* dates mein se
kaunsi doosri ke upar layer hoti hai. Ye us waqt bhi print hota tha jab sirf **ek** hi
manual date hoti, jahan kuch bhi layer nahi ho raha aur "anchor" lafz ka koi matlab nahi
hai.
**Badlaao:** ye ab sirf tab print hota hai jab `len(selected_dates) > 1`. Do test runs se
live confirm kiya, dono ek hi real AOI par: ek manual date → koi anchor warning print
nahi hui; do manual dates → wahi warning bilkul sahi tareeqe se print hui, kyunke wahan
asal mein top/bottom ka rishta hai.

---

## Bug 8: Acreage poore mulk ke liye ek fixed map zone se measure ho rahi thi — pehle fix kiya, phir aapki request par wapas kar diya

**Kahan:** `postprocess.py` aur `qc.py`, constant `AREA_CRS_EPSG = 32642`.

**`32642` concretely kya hai:** "UTM Zone 42 North" ka code — ek map projection jo
exactly 69°E longitude ki lakeer par sahi hai, aur us lakeer se jitna mashriq ya maghrib
jayein, utni hi kam sahi hoti jaati hai.

**Real numbers, is testing ke liye di gayi asal AOI files se:**
- Faisalabad ke qareeb wali cane field 73.41°E par hai. Ye zone 42N mein **nahi** hai —
  ye "UTM Zone 43 North" (`EPSG:32643`, 75°E par sahi) mein hai. Isay zone 42N ke hisaab
  se measure karne se **7,834.7495 acres** milta. Sahi (zone 43N) measurement:
  **7,805.3162 acres**. Matlab **29.43 acres zyada, +0.377%**, ghalat zone use karne se.
- Sheikhupura ke qareeb wali wheat field 74.04°E par hai — ye bhi zone 43N, aur zone
  42N ki sahi lakeer se cane field se bhi zyada door. Purana (ghalat-zone) measurement:
  **25,378.2466 acres**. Sahi measurement: **25,240.0209 acres**. Matlab **138.2 acres
  zyada, +0.548%**.

**Pehle kya badla gaya tha:** ek function add kiya (`aoi_io.py` mein `local_utm_epsg`)
jo location ke asal longitude/latitude se sahi UTM zone khud calculate karta hai (`zone =
int((lon + 180) / 6) + 1`, phir Northern ya Southern-hemisphere EPSG code chunta hai),
aur `postprocess.py` aur `qc.py` ko fixed `32642` ke bajaye ye use karne ke liye wire
kiya. Live confirm kiya: `qc._aoi_acres()` ne cane field par exactly **7,805.3162**
diya — independently calculate kiye hue sahi number se match karta hua.

**Phir aapki request par explicitly wapas kar diya:** `postprocess.py` aur `qc.py` ko
wapas hamesha `AREA_CRS_EPSG = 32642` use karne par badal diya, chahe AOI kahin bhi ho.
Revert ke baad live confirm kiya: `qc._aoi_acres()` ne wahi cane field par ab dobara
**7,834.7495** diya — original number, waisa hi jaisa is testing shuru hone se pehle
tha.

**Ek cheez jo jaan-boojh kar revert **nahi** hui, aapke apne faisle se:**
`static_classify.py` ka `_estimate_aoi_pixels()` function (jo sirf ek internal sanity
check ke liye use hota hai — kya crop mask degenerate lag raha hai — koi asal acreage
number nahi jo operator dekhta hai) is testing shuru hone se **pehle hi**, is codebase ke
bilkul pehle commit se, apni sahi UTM zone khud calculate karta tha. Wo waisa hi rakha
gaya jaisa hamesha se tha; ye sawal explicitly poocha gaya aur jawab tha isay chhod dena.

---

## Jo masail mile, aur jaan-boojh kar chhue nahi — har ek ki khaas wajah ke sath

**1. `run.py` ke command-line flags 2016 se pehle ke saalon ke liye ghalat satellite
source use karte hain by default.** `run.py` apne `argparse` setup mein `--ndvi-source`
aur `--static-source` ko `default="stac"` deta hai, aur ye default value hamesha config
mein bhej deta hai, chahe aap ne flag type kiya ho ya nahi. Alag se, `config.py` mein
logic hai jo 2016 se pehle ke saalon ke liye khud `ndvi_source="gee"` par switch karne ke
liye hai — lekin sirf tab jab caller ne `ndvi_source` bilkul mention na kiya ho. Kyunke
`run.py` hamesha isay mention karta hai (as "stac", default), wo automatic switch
command-line se chalane par kabhi trigger nahi hoti. Real nateeja: `python run.py --crop
cane --year 2014 ...` (bina `--ndvi-source` flag type kiye) crash ho jata hai:
`ValueError: ndvi_source='stac' but Sentinel-2 does not cover 2014 (archive starts
2016)...`. Wahi config Python se direct call karna (`run.py`'s command line skip karke)
ye masla **nahi** rakhta — wahan automatic switch sahi kaam karti hai. **Jaan-boojh kar,
aapki hidayat ke mutabiq 2014-2017 wale kaam ko rukwane ke liye, fix nahi kiya.**

**2. Purani date-folders static-run folder mein ikhtiya ho jaati hain jab aap date badal
dein.** Agar aap static-image stage chalayen, phir usi district ke liye alag date se
dobara chalayen, purani date ka output folder **aur** nayi date ka output folder dono
ek hi run folder ke andar ikhtiya rehte hain (misal ke taur par
`18_Oct_2025_and_10_Nov_2025/` folder `16_Oct_2025/` ke sath). Ye pehle asal khatra
tha — tool ghalat date ke results wapas de sakta tha. Wo khaas khatra pehle hi kisi
doosri jagah fix ho gaya hai (tool ab check karta hai kaunsi raster file ne asal mein wo
output banaya, phir hi reuse karta hai). Baqi sirf ye reh gaya hai ke purana folder delete
nahi hota. **Jaan-boojh kar chhoda,** kyunke folder khud-ba-khud delete karna khatra hai
— shayad koi comparison ke liye rakhna chahta ho, jo halke messy folder se zyada bura
hai.

**3. `static_model_memory_expansion = 12.0`.** Jab andaza lagana hai kitne parallel
workers safely ek bara static-image model chala sakte hain, code har worker ki memory
istemal ka andaza "model ki file size × 12" se lagata hai. Pehle test campaigns ke teen
real models par measure kiya: wheat ka model **15.7×** phaila (0.7 MB file → 11 MB
resident), cane ka **10.4×** (27.5 MB → 285 MB), aur spring maize ka **9.2×** (563 MB →
5,165 MB). Is liye `12.0` ek reasonable darmiyani value hai lekin har model ke liye
measure nahi hui. **Waisa hi chhoda,** ek na-measure-hui guess ko doosri guess se badalne
ke bajaye.

---

## Ek naya masla, isi session mein mila, likh liya, jaan-boojh kar abhi fix nahi kiya

**Exact trigger:** koi bhi crop, saal 2016 ya 2017, `ndvi_source="gee"` (jo Landsat 8
imagery, 30-metre pixels use karta hai) **aur** `static_source="stac"` (jo Sentinel-2
imagery, 10-metre pixels use karta hai) ek sath — ye combination sirf in do saalon mein
mumkin hai.

**Kya hua, real numbers ke sath:** spr_maize, saal 2016, AOI "6" ko isi exact
combination ke sath chalaya, tool ki apni result-check ne report kiya:
`static_retention_pct = 277.9%` aur warning: `"The static model kept 277.9% of the NDVI
stage's crop area -- effectively all of it..."`. 100% se zyada percentage is number ke
matlab ke hisaab se mathematically impossible hai (static stage sirf un pixels ko
confirm ya reject kar sakta hai jo NDVI stage ne pehle hi flag kiye — usse **zyada**
kabhi nahi ho sakta).

**Exact wajah, raw numbers check karke mili:** NDVI stage (Landsat, 30m pixels) ne
`ndvi_crop_pixels = 1497` report kiya. Static stage (Sentinel-2, 10m pixels) ne
`static_crop_pixels = 4160` report kiya. `qc.py` ka calculation sirf `100 *
static_crop_pixels / ndvi_crop_pixels` hai — ek pixel count ko doosre se divide karna,
bina ye jaane ke ek Landsat pixel Sentinel-2 pixel se **9 guna** zyada zameen cover
karta hai (30m × 30m = 900 sq m, versus 10m × 10m = 100 sq m). Dono counts ko asal
zameeni area mein convert karein: `1497 × 900 sq m = 332.9 acres` (NDVI) versus `4160 ×
100 sq m = 102.8 acres` (static) — jo sensible **30.9%** deta hai, 277.9% nahi.

**Status:** yahan aur `TEST_REPORT_2026-09-16.md` mein likh liya hai, yaad rakha jayega.
**Fix nahi kiya** — 2014-2017 ke saalon ki is session ki testing `run_static_model=False`
(static stage bilkul band) ke sath ki gayi thi, jo abhi is khaas bug se bacha leti hai
kyunke ye sirf tab hota hai jab static stage asal mein chale.

---

## Upar ki sab baaton ko confirm karne ke liye ki gayi testing

- Automatic offline test suite (`python tests/run_all.py`) is kaam ki shuruaat mein
  **81 passing checks** se lekar aakhir mein **112 passing checks** tak gayi — 31 naye
  checks upar ki har fix ko cover karte hain, aur is waqt sab pass hote hain (`112
  passed, 0 failed`).
- Sirf offline checks se aage, teen real boundary files par asal end-to-end pipeline
  runs chalaye gaye:
  - **Cane**, `fao_cane_validation_aoi_1.shp` (Faisalabad ke qareeb, 73.41°E) — STAC
    imagery se dono stages, phir dobara Google Earth Engine se static stage, phir har
    fix ke baad dobara — har baar 203 matching crop-area features.
  - **Wheat**, `3.shp` (Sheikhupura ke qareeb, 74.04°E, region=punjab) — STAC se dono
    stages (728 features, 13,240.8 acres) aur dobara GEE se static stage (628 features,
    14,506.0 acres).
  - **Spring maize**, `fao_spr_maize_validation_aoi_6.shp` — saal 2014, 2015, 2016 aur
    2017 ke liye chalaya, Landsat imagery (`ndvi_source="gee"`) aur static stage band
    karke, bilkul jaise ye crop asal mein in saalon ke liye use hota hai; sab 4 saal koi
    error ya warning ke bina mukammal hue.
