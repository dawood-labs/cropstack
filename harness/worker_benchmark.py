"""Isolated benchmark of the NDVI inference stage across worker settings.

Reuses the pipeline's own `worker_process_tile` and the same ProcessPoolExecutor shape
as `ndvi_pipeline.run_ndvi_pipeline`, so the numbers describe the real code path -- only
`ndvi_worker_count` and `ndvi_worker_max_tasks` vary. Predictions go to a throwaway
directory each time so no run is measuring a resume.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

REPO = Path("/home/jovyan/FAO/optimized_code_testing/cropstack")
HARNESS = Path("/home/jovyan/FAO/optimized_code_testing/harness")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HARNESS))

from instrument import ResourceSampler  # noqa: E402
from inference_workers import worker_process_tile  # noqa: E402

MODEL = ("/home/jovyan/.cache/fao_pipeline/models/farmdar_data_catalog/"
         "fao_cane_model_file/fao_cane_rf_model.joblib")
INFER_START, INFER_END = "2025-01-01", "2025-11-15"


def run_once(tiles, workers, max_tasks, scratch, metrics_dir, tag):
    predictions = scratch / f"pred_w{workers}_m{max_tasks}"
    shutil.rmtree(predictions, ignore_errors=True)
    predictions.mkdir(parents=True, exist_ok=True)

    sampler = ResourceSampler(
        csv_path=metrics_dir / f"bench_{tag}_w{workers}_m{max_tasks}_samples.csv",
        watch_dir=predictions).start()
    sampler.mark(f"infer_w{workers}_m{max_tasks}")
    started = time.time()
    ok, failed = 0, 0
    with ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=max_tasks,
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {pool.submit(worker_process_tile, tile, predictions, MODEL,
                               INFER_START, INFER_END, 0.5, 2, (-1.0, 1.0), 255, False): tile
                   for tile in tiles}
        for future in as_completed(futures):
            try:
                future.result(timeout=900)
                ok += 1
            except Exception as exc:
                failed += 1
                print(f"    tile failed: {exc}")
    elapsed = time.time() - started
    sampler.stop()
    summary = sampler.summary()
    shutil.rmtree(predictions, ignore_errors=True)
    return {
        "workers": workers, "max_tasks_per_child": max_tasks,
        "tiles": len(tiles), "ok": ok, "failed": failed,
        "wall_s": round(elapsed, 1),
        "peak_rss_tree_mb": summary["peak_rss_tree_mb"],
        "peak_rss_single_proc_mb": summary["peak_rss_single_proc_mb"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tiles-dir", required=True)
    parser.add_argument("--tag", default="cane")
    parser.add_argument("--settings", default="1:1,2:1,4:8,6:8,8:8")
    args = parser.parse_args()

    tiles = sorted(Path(args.tiles_dir).glob("sentinel_*m_tile_*.tif"))
    if not tiles:
        print(f"no tiles under {args.tiles_dir}")
        return 1
    print(f"{len(tiles)} tile(s) from {args.tiles_dir}")
    if not Path(MODEL).exists():
        print(f"model not cached at {MODEL}")
        return 1

    metrics_dir = Path("/home/jovyan/FAO/optimized_code_testing/metrics")
    scratch = Path("/tmp/claude-1000/-home-jovyan-FAO-optimized-code-testing/"
                   "d4421094-41a5-461c-b80f-491152d02357/scratchpad/bench")
    scratch.mkdir(parents=True, exist_ok=True)

    results = []
    for setting in args.settings.split(","):
        workers, max_tasks = (int(x) for x in setting.split(":"))
        print(f"\n--- workers={workers} max_tasks_per_child={max_tasks} ---", flush=True)
        row = run_once(tiles, workers, max_tasks, scratch, metrics_dir, args.tag)
        results.append(row)
        print(f"    wall {row['wall_s']}s  peak tree RSS {row['peak_rss_tree_mb']} MB  "
              f"peak proc {row['peak_rss_single_proc_mb']} MB  ok={row['ok']} failed={row['failed']}",
              flush=True)

    out = metrics_dir / f"worker_benchmark_{args.tag}.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\n{'workers':>8}{'max_tasks':>11}{'wall_s':>9}{'peak_tree_MB':>14}{'peak_proc_MB':>14}")
    for row in results:
        print(f"{row['workers']:>8}{row['max_tasks_per_child']:>11}{row['wall_s']:>9.1f}"
              f"{row['peak_rss_tree_mb']:>14.0f}{row['peak_rss_single_proc_mb']:>14.0f}")
    baseline = next((r for r in results if r["workers"] == 1), None)
    if baseline:
        print("\nspeedup vs 1 worker:")
        for row in results:
            print(f"  {row['workers']} workers: {baseline['wall_s']/row['wall_s']:.2f}x")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
