"""`batch.py` runs jobs strictly sequentially (see its own module docstring), so it must
size each job's worker pool as one district running alone -- not as `len(jobs)` districts
sharing the machine concurrently, which never actually happens.

Regression test for N-2 (2026-09-16 audit): before the fix, `run_batch` called
`resources.plan_resources(district_count=len(jobs))`, so a batch of 10 districts on an
8-core/61 GiB box divided the machine into `ndvi=2, static=2` per job -- 2/7ths of what a
single district gets running alone -- even though the jobs never overlap in time.

The jobs below all fail immediately (no AOI on disk) -- only `resources.plan_resources`'s
call arguments matter here, and that call happens once, before the job loop even starts.
"""
from __future__ import annotations
import logging

import batch
import resources


def run(check):
    captured = {}
    real_plan_resources = resources.plan_resources

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_plan_resources(*args, **kwargs)

    batch.resources.plan_resources = _spy
    logging.disable(logging.CRITICAL)
    try:
        for job_count in (1, 5, 10):
            captured.clear()
            jobs = [{"crop": "cane", "year": "2025", "district_name": f"d{i}"}
                    for i in range(job_count)]
            batch.run_batch(jobs, continue_on_error=True)
            check(f"batch of {job_count} job(s) plans for exactly 1 district, not {job_count}",
                  captured.get("district_count") == 1,
                  f"district_count={captured.get('district_count')}")
    finally:
        logging.disable(logging.NOTSET)
        batch.resources.plan_resources = real_plan_resources

    # An explicit plan, or auto_resources=False, must still be honoured untouched -- the
    # fix must not remove either escape hatch.
    plan = resources.plan_resources(district_count=1, cores=8, available_memory_gib=61.0)
    results = batch.run_batch([], plan=plan)
    check("an explicit plan short-circuits auto-sizing", results == [])

    results = batch.run_batch([], auto_resources=False)
    check("auto_resources=False runs without computing a plan at all", results == [])
