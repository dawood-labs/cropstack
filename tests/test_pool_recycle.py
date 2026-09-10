"""FAIL-13: NDVI inference must not deadlock when workers are recycled.

The Kasur district run stopped dead at 48 of 57 tiles -- exactly
`6 workers x ndvi_worker_max_tasks=8`. Every worker was gone and the parent sat in
futex_wait forever. `ProcessPoolExecutor(max_tasks_per_child=...)` on Python 3.11.15
with the spawn context deadlocks at that boundary; `check_cpython_bug_still_present`
below reproduces it in isolation so the day it is fixed upstream is visible.

Invisible on the 4-tile Okara AOI, because 4 < 48. This is the first test in the repo
whose shape had to come from production scale.
"""
from __future__ import annotations

import multiprocessing
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import ndvi_pipeline

# The district run's exact shape.
WORKERS = 6
MAX_TASKS = 8
TILES = 57
DEADLOCK_AT = WORKERS * MAX_TASKS   # 48


def double(value):
    """Module level so spawn can import it."""
    return {"prediction": value * 2}


def sleep_forever(value):
    time.sleep(3600)
    return {"prediction": value}


def fail_always(value):
    raise ValueError(f"tile {value} is broken")


CPYTHON_REPRO = f'''
import multiprocessing, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
def work(i):
    return i * 2
if __name__ == "__main__":
    with ProcessPoolExecutor(max_workers={WORKERS}, max_tasks_per_child={MAX_TASKS},
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(work, i) for i in range({TILES})]
        for f in as_completed(futures):
            f.result()
    print("COMPLETED")
'''


def run(check):
    # ---------------------------------------------- the shape that used to deadlock
    started = time.time()
    results, failed = ndvi_pipeline.run_tile_inference(
        list(range(TILES)), double, lambda tile: (tile,),
        worker_count=WORKERS, max_tasks_per_worker=MAX_TASKS, tile_timeout_s=60)
    elapsed = time.time() - started
    check(f"{TILES} tiles across {WORKERS} workers recycling every {MAX_TASKS} completes",
          len(results) == TILES and not failed, f"{len(results)}/{TILES} in {elapsed:.1f}s")
    check("it got past the old deadlock boundary",
          len(results) > DEADLOCK_AT, f"{len(results)} > {DEADLOCK_AT}")
    check("every result came back, in some order",
          sorted(r["prediction"] for r in results) == [i * 2 for i in range(TILES)])

    # Batching is what bounds worker memory now, so it must actually batch.
    check("work is split into batches of workers x max_tasks",
          (TILES + DEADLOCK_AT - 1) // DEADLOCK_AT == 2, "57 tiles -> 2 batches of 48")

    # ------------------------------------------------- a stall fails, does not hang
    started = time.time()
    stalled = None
    try:
        ndvi_pipeline.run_tile_inference(
            [0, 1], sleep_forever, lambda tile: (tile,),
            worker_count=2, max_tasks_per_worker=1, tile_timeout_s=3)
    except RuntimeError as exc:
        stalled = str(exc)
    elapsed = time.time() - started
    check("a stalled batch raises instead of hanging", stalled is not None,
          (stalled or "no exception")[:80])
    check("it gives up close to the budget, not after the task finishes",
          elapsed < 60, f"{elapsed:.1f}s against a 3600s task")
    check("the error tells the operator to resume rather than restart",
          stalled is not None and "run_mode='resume'" in stalled)

    # --------------------------------------------------- a broken tile is not fatal
    results, failed = ndvi_pipeline.run_tile_inference(
        list(range(4)), fail_always, lambda tile: (tile,),
        worker_count=2, max_tasks_per_worker=2, tile_timeout_s=60)
    check("tiles that raise are collected, not swallowed and not fatal",
          results == [] and len(failed) == 4, f"results={len(results)} failed={len(failed)}")

    # ------------------------------------- the upstream bug this works around
    script = Path(__file__).parent / "_cpython_pool_repro.py"
    script.write_text(CPYTHON_REPRO)
    try:
        proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                              text=True, timeout=90)
        still_broken = "COMPLETED" not in proc.stdout
    except subprocess.TimeoutExpired:
        still_broken = True
    finally:
        script.unlink(missing_ok=True)
    check("upstream ProcessPoolExecutor(max_tasks_per_child) still deadlocks here "
          "-- if this fails, CPython is fixed and the workaround can be reconsidered",
          still_broken, f"python {sys.version.split()[0]}")
