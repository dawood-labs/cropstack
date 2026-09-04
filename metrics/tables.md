### scenarios

| scenario | status | wall (min) | NDVI (min) | static (min) | peak RAM (GiB) | peak disk (MB) | net in (MB) | features | acres | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| `A1_cane_2016` | ok | 17.26 | 16.7 | 0.5 | 5.55 | 319 | 40274 | 269 | 768.49 | PASS (warn) — NDVI map contains 6 nodata (255) pixels (0.0% of the grid) -- tiles with no usable signal in the inference window |
| `A1_cane_2025` | ok | 9.11 | 7.6 | 1.3 | 5.72 | 646 | 5043 | 932 | 2120.35 | PASS |
| `A1_spr_maize_2025` | ok | 7.21 | 4.8 | 1.7 | 27.89 | 415 | 4334 | 335 | 833.87 | PASS |
| `A1_wheat_2025` | ok | 6.97 | 6.5 | 0.3 | 5.71 | 633 | 1819 | 2598 | 15475.72 | PASS |
| `A2_cane_2016` | ok | 0.70 | 0.0 | 0.7 | 3.02 | 1 | 505 | 184 | 488.34 | PASS (warn) — NDVI map contains 6 nodata (255) pixels (0.0% of the grid) -- tiles with no usable signal in the inference window |
| `A2_cane_2025` | ok | 1.78 | 0.0 | 1.8 | 3.45 | 55 | 2996 | 247 | 465.5 | PASS (warn) — vector keeps only 0.49x its source raster's in-AOI crop area -- heavily fragmented classification (see the static-sieve no-op finding) |
| `A2_spr_maize_2025` | ok | 1.55 | 0.0 | 1.5 | 25.57 | 20 | 1191 | 308 | 1140.05 | PASS |
| `A2_wheat_2025` | ok | 1.53 | 0.0 | 1.5 | 2.54 | 46 | 2647 | 2598 | 15475.72 | PASS |
| `A3_cane_2016` | ok | 0.83 | 0.0 | 0.8 | 0.86 | 1 | 6 | 11 | 9.93 | PASS (warn) — NDVI map contains 6 nodata (255) pixels (0.0% of the grid) -- tiles with no usable signal in the inference window; vector keeps only 0.27x its source raster's in-AOI crop area -- heavily fragmented classification (see the static-sieve no-op finding) |
| `A3_cane_2025` | ok | 2.04 | 0.0 | 2.0 | 0.90 | 42 | 40 | 788 | 1808.14 | PASS |
| `A3_spr_maize_2025` | ok | 2.80 | 0.0 | 2.7 | 6.48 | 39 | 39 | 336 | 833.43 | PASS |
| `A3_wheat_2025` | ok | 3.08 | 0.0 | 3.0 | 0.75 | 50 | 42 | 2596 | 15456.45 | PASS |
| `A4_cane_2016` | failed | 0.00 | — | — | 0.27 | 1 | 1 | — | — | **RUN FAILED** |
| `A4_cane_2025` | ok | 1.99 | 0.0 | 1.9 | 0.88 | 43 | 40 | 788 | 1808.14 | PASS |
| `A4_spr_maize_2025` | ok | 2.59 | 0.0 | 2.5 | 6.47 | 14 | 13 | 118 | 507.72 | PASS |
| `A4_wheat_2025` | ok | 2.04 | 0.0 | 2.0 | 0.75 | 55 | 42 | 2381 | 17684.22 | PASS |
| `A4b_cane_2016_landsat_dates` | ok | 0.88 | 0.0 | 0.8 | 0.86 | 1 | 5 | 11 | 9.93 | PASS (warn) — NDVI map contains 6 nodata (255) pixels (0.0% of the grid) -- tiles with no usable signal in the inference window; vector keeps only 0.27x its source raster's in-AOI crop area -- heavily fragmented classification (see the static-sieve no-op finding) |
| `A5_cane_2025_manual_gcs` | ok | 0.13 | 0.0 | 0.1 | 0.84 | 139 | 39 | 788 | 1808.14 | PASS |
| `B_rice_2025` | ok | 5.61 | 5.5 | — | 4.56 | 536 | 1247 | 1269 | 28498.8 | PASS |
| `C2_second_resume` | ok | 0.39 | 0.0 | 0.4 | 3.08 | 56 | 55 | 247 | 465.5 | **FAIL** — STALE vector: the vector stage reused an existing GPKG and its acreage is only 0.19x this run's own source raster (465.5 vs 2437.0 acres in AOI) |
| `C3_new` | ok | 8.15 | 7.8 | 0.3 | 5.73 | 651 | 2034 | 932 | 2120.35 | PASS |
| `C4b_resume_after_kill` | ok | 1.39 | 1.0 | 0.3 | 3.30 | 700 | 50 | 932 | 2120.35 | PASS |
| `C6_ndvi_resume_static_new` | ok | 0.38 | 0.0 | 0.4 | 2.99 | 56 | 55 | 932 | 2120.35 | PASS |
| `D_geojson_cane_2025` | ok | 7.79 | 7.8 | — | 5.89 | 646 | 1986 | 1309 | 3134.64 | PASS |
| `D_gpkg_cane_2025` | ok | 7.94 | 7.9 | — | 5.81 | 646 | 2001 | 1309 | 3134.64 | PASS |
| `G_gee_ndvi_cane_2025` | ok | 23.63 | 21.0 | 2.6 | 4.02 | 446 | 495 | 808 | 1825.34 | PASS (warn) — NDVI map contains 316,470 nodata (255) pixels (9.7% of the grid) -- tiles with no usable signal in the inference window |
| `W3_tiledeg02_cane_2025` | ok | 7.47 | 7.5 | — | 6.84 | 644 | 1637 | 1309 | 3134.66 | PASS |
| `W4_baseline_ndvionly_cane_2025` | ok | 7.64 | 7.6 | — | 5.77 | 646 | 2109 | 1309 | 3134.64 | PASS |
| `W5_stacworkers2_cane_2025` | ok | 8.63 | 8.6 | — | 5.17 | 646 | 1975 | 1309 | 3134.64 | PASS |
| `X1_stac_static_keep` | ok | 0.53 | 0.0 | 0.5 | 3.11 | 61 | 83 | 247 | 465.5 | PASS (warn) — vector keeps only 0.49x its source raster's in-AOI crop area -- heavily fragmented classification (see the static-sieve no-op finding) |
| `X2_gee_static_keep` | ok | 1.95 | 0.0 | 1.9 | 0.87 | 100 | 40 | 788 | 1808.14 | PASS |
| `X3_stac_18oct_only` | ok | 0.20 | 0.0 | 0.2 | 2.86 | 102 | 12 | 145 | 365.56 | PASS |
| `X4_stac_10nov_only` | ok | 0.31 | 0.0 | 0.3 | 2.99 | 156 | 49 | 795 | 1817.94 | PASS |
| `X5_stac_16oct_only` | ok | 0.29 | 0.0 | 0.3 | 2.92 | 157 | 47 | 932 | 2120.35 | PASS |

### stage_seconds

| scenario | `ndvi_acquire_stac` | `ndvi_acquire_gee` | `ndvi_mosaic` | `static_acquire_stac` | `static_acquire_gee` | `static_crop_mask` | `static_classify` | `sieve` | `vector_stage` | `resolve_models` |
|---|---|---|---|---|---|---|---|---|---|---|
| `A1_cane_2016` | 967s | — | 0s | 29s | — | 0s | 3s | 1s | 0s | 0s |
| `A1_cane_2025` | 417s | — | 0s | 70s | — | 0s | 5s | 1s | 1s | 15s |
| `A1_spr_maize_2025` | 264s | — | 0s | 70s | — | 0s | 32s | 1s | 1s | 42s |
| `A1_wheat_2025` | 382s | — | 0s | 14s | — | 0s | 4s | 1s | 1s | 6s |
| `A2_cane_2016` | — | — | — | 38s | — | 0s | 3s | 0s | 0s | 0s |
| `A2_cane_2025` | — | — | — | 101s | — | 0s | 5s | 0s | 1s | 0s |
| `A2_spr_maize_2025` | — | — | — | 74s | — | 0s | 18s | 0s | 0s | 0s |
| `A2_wheat_2025` | — | — | — | 86s | — | 0s | 4s | 0s | 1s | 0s |
| `A3_cane_2016` | — | — | — | — | 46s | 0s | 2s | 0s | 0s | 0s |
| `A3_cane_2025` | — | — | — | — | 114s | 0s | 4s | 0s | 0s | 0s |
| `A3_spr_maize_2025` | — | — | — | — | 136s | 0s | 28s | 0s | 0s | 0s |
| `A3_wheat_2025` | — | — | — | — | 177s | 0s | 4s | 0s | 1s | 0s |
| `A4_cane_2016` | — | — | — | — | 23s | — | — | 0s | — | 0s |
| `A4_cane_2025` | — | — | — | — | 110s | 0s | 4s | 0s | 0s | 0s |
| `A4_spr_maize_2025` | — | — | — | — | 135s | 0s | 15s | 0s | 0s | 0s |
| `A4_wheat_2025` | — | — | — | — | 114s | 0s | 3s | 0s | 1s | 0s |
| `A4b_cane_2016_landsat_dates` | — | — | — | — | 46s | 0s | 2s | 0s | 0s | 0s |
| `A5_cane_2025_manual_gcs` | — | — | — | — | 3s | 0s | 4s | 0s | 0s | 0s |
| `B_rice_2025` | 316s | — | 0s | — | — | — | — | 1s | 1s | 3s |
| `C2_second_resume` | — | — | — | 18s | — | 0s | 5s | 0s | 0s | 0s |
| `C3_new` | 428s | — | 0s | 15s | — | 0s | 5s | 1s | 1s | 0s |
| `C4b_resume_after_kill` | 0s | — | 0s | 16s | — | 0s | 5s | 1s | 0s | 0s |
| `C6_ndvi_resume_static_new` | — | — | — | 17s | — | 0s | 5s | 1s | 1s | 0s |
| `D_geojson_cane_2025` | 427s | — | 0s | — | — | — | — | 1s | 1s | 0s |
| `D_gpkg_cane_2025` | 437s | — | 0s | — | — | — | — | 1s | 1s | 0s |
| `G_gee_ndvi_cane_2025` | — | 1233s | 0s | — | 151s | 0s | 4s | 0s | 0s | 0s |
| `W3_tiledeg02_cane_2025` | 326s | — | 0s | — | — | — | — | 1s | 1s | 0s |
| `W4_baseline_ndvionly_cane_2025` | 419s | — | 0s | — | — | — | — | 1s | 1s | 0s |
| `W5_stacworkers2_cane_2025` | 477s | — | 0s | — | — | — | — | 1s | 1s | 0s |
| `X1_stac_static_keep` | — | — | — | 26s | — | 0s | 5s | 0s | 1s | 0s |
| `X2_gee_static_keep` | — | — | — | — | 111s | 0s | 4s | 0s | 0s | 0s |
| `X3_stac_18oct_only` | — | — | — | 8s | — | 0s | 3s | 0s | 0s | 0s |
| `X4_stac_10nov_only` | — | — | — | 13s | — | 0s | 5s | 0s | 1s | 0s |
| `X5_stac_16oct_only` | — | — | — | 12s | — | 0s | 5s | 0s | 1s | 0s |

### stage_peak_rss_gib

| scenario | `ndvi_acquire_stac` | `ndvi_acquire_gee` | `ndvi_mosaic` | `static_acquire_stac` | `static_acquire_gee` | `static_crop_mask` | `static_classify` | `sieve` | `vector_stage` | `resolve_models` |
|---|---|---|---|---|---|---|---|---|---|---|
| `A1_cane_2016` | 5.10 | — | 2.65 | 2.86 | — | — | 4.87 | 2.64 | 2.64 | 0.20 |
| `A1_cane_2025` | 5.45 | — | — | 3.36 | — | — | 5.44 | 3.25 | 3.25 | 0.26 |
| `A1_spr_maize_2025` | 4.25 | — | — | 3.42 | — | — | 27.89 | 2.82 | 3.31 | 0.26 |
| `A1_wheat_2025` | 5.71 | — | — | 3.42 | — | — | 4.67 | 3.35 | 3.35 | 0.24 |
| `A2_cane_2016` | — | — | — | 0.98 | — | — | 3.02 | 0.85 | 0.85 | 0.20 |
| `A2_cane_2025` | — | — | — | 1.18 | — | — | 3.45 | 1.16 | 1.16 | 0.20 |
| `A2_spr_maize_2025` | — | — | — | 1.03 | — | — | 25.57 | 0.99 | 0.99 | 0.20 |
| `A2_wheat_2025` | — | — | — | 1.24 | — | — | 2.54 | 1.23 | 1.23 | 0.20 |
| `A3_cane_2016` | — | — | — | — | 0.27 | — | 0.86 | — | — | 0.20 |
| `A3_cane_2025` | — | — | — | — | 0.30 | — | 0.90 | — | 0.36 | — |
| `A3_spr_maize_2025` | — | — | — | — | 0.29 | — | 6.48 | — | 0.35 | 0.20 |
| `A3_wheat_2025` | — | — | — | — | 0.30 | — | 0.75 | 0.34 | 0.35 | 0.20 |
| `A4_cane_2016` | — | — | — | — | 0.27 | — | — | — | — | 0.20 |
| `A4_cane_2025` | — | — | — | — | 0.29 | — | 0.88 | — | 0.35 | 0.20 |
| `A4_spr_maize_2025` | — | — | — | — | 0.27 | — | 6.47 | 0.33 | — | 0.20 |
| `A4_wheat_2025` | — | — | — | — | 0.30 | 0.30 | 0.75 | 0.33 | 0.35 | 0.20 |
| `A4b_cane_2016_landsat_dates` | — | — | — | — | 0.27 | — | 0.86 | — | — | 0.20 |
| `A5_cane_2025_manual_gcs` | — | — | — | — | 0.25 | — | 0.84 | 0.20 | 0.31 | 0.20 |
| `B_rice_2025` | 4.56 | — | — | — | — | — | — | 2.17 | 2.17 | 0.25 |
| `C2_second_resume` | — | — | — | 0.92 | — | — | 3.08 | 0.89 | — | 0.20 |
| `C3_new` | 5.17 | — | — | 3.28 | — | — | 5.36 | 3.17 | 3.17 | 0.20 |
| `C4b_resume_after_kill` | 0.20 | — | 0.27 | 1.15 | — | — | 3.30 | — | 1.06 | 0.20 |
| `C6_ndvi_resume_static_new` | — | — | — | 0.94 | — | — | 2.99 | 0.79 | 0.79 | 0.20 |
| `D_geojson_cane_2025` | 5.84 | — | — | — | — | — | — | 2.95 | 2.95 | 0.20 |
| `D_gpkg_cane_2025` | 5.40 | — | — | — | — | — | — | 2.85 | 2.85 | 0.20 |
| `G_gee_ndvi_cane_2025` | — | 0.33 | — | — | 0.45 | — | 1.00 | — | 0.46 | 0.20 |
| `W3_tiledeg02_cane_2025` | 6.84 | — | — | — | — | — | — | 1.72 | 1.72 | 0.20 |
| `W4_baseline_ndvionly_cane_2025` | 5.68 | — | — | — | — | — | — | 2.80 | 2.80 | 0.20 |
| `W5_stacworkers2_cane_2025` | 4.30 | — | — | — | — | — | — | 2.32 | 2.14 | 0.20 |
| `X1_stac_static_keep` | — | — | — | 0.97 | — | — | 3.11 | 0.93 | 0.93 | 0.20 |
| `X2_gee_static_keep` | — | — | — | — | 0.29 | — | 0.87 | 0.35 | — | 0.20 |
| `X3_stac_18oct_only` | — | — | — | 0.74 | — | — | 2.86 | 0.63 | 0.65 | 0.20 |
| `X4_stac_10nov_only` | — | — | — | 0.85 | — | — | 2.99 | 0.77 | 0.77 | 0.20 |
| `X5_stac_16oct_only` | — | — | — | 0.82 | — | — | 2.92 | 0.74 | 0.74 | 0.20 |