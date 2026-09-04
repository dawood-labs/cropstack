"""Sizing a run for the machine it is on, and the static memory term that was wrong.

Kasur peaked at 8.8 GiB on a 648 KB model, because the pool was sized from the model
while the cost was the windows. Both halves are checked here against the real numbers:
the Kasur shape (6 bands, uint16, chunk 2048) and the box it ran on (8 cores, 61 GiB).
"""
from __future__ import annotations

import resources
import static_classify

KASUR_BANDS, KASUR_DTYPE_SIZE, KASUR_CHUNK = 6, 2, 2048
TINY_MODEL = "tests/_tiny_model_probe.json"   # stands in for the 648 KB wheat model


def run(check):
    # ------------------------------------------------ the window term, which was missing
    window_gib = static_classify.window_working_bytes(
        KASUR_CHUNK, KASUR_BANDS, KASUR_DTYPE_SIZE) / 2**30
    check("a Kasur-shaped window is sized in the hundreds of MiB, not zero",
          0.1 < window_gib < 1.0, f"{window_gib:.2f} GiB per worker per window")
    check("halving the chunk quarters the window cost",
          abs(static_classify.window_working_bytes(1024, KASUR_BANDS, KASUR_DTYPE_SIZE) * 4
              - static_classify.window_working_bytes(2048, KASUR_BANDS, KASUR_DTYPE_SIZE)) == 0)

    # A tiny model with a big window: the old sizing saw "0 GiB per worker" and allowed
    # every core. The window term has to be what bounds it.
    workers = static_classify.resolve_worker_count(
        requested=32, window_count=1000, model_path="/nonexistent-model.json",
        memory_fraction=0.5, per_worker_window_bytes=static_classify.window_working_bytes(
            KASUR_CHUNK, KASUR_BANDS, KASUR_DTYPE_SIZE),
        memory_budget_bytes=int(8 * 2**30))
    check("a tiny model no longer means unlimited workers",
          workers < 32, f"{workers} workers in an 8 GiB budget")
    check("and the bound is the window arithmetic, not a guess",
          workers == max(1, int((8 * 2**30 * 0.5) // static_classify.window_working_bytes(
              KASUR_CHUNK, KASUR_BANDS, KASUR_DTYPE_SIZE))), f"{workers}")

    smaller_chunk = static_classify.resolve_worker_count(
        requested=32, window_count=1000, model_path="/nonexistent-model.json",
        memory_fraction=0.5, per_worker_window_bytes=static_classify.window_working_bytes(
            1024, KASUR_BANDS, KASUR_DTYPE_SIZE),
        memory_budget_bytes=int(8 * 2**30))
    check("a smaller chunk buys more workers in the same RAM",
          smaller_chunk > workers, f"{smaller_chunk} at chunk 1024 vs {workers} at 2048")

    check("an explicit budget overrides what the machine happens to have free",
          static_classify.resolve_worker_count(
              requested=8, window_count=100, model_path="/nonexistent-model.json",
              per_worker_window_bytes=2**30, memory_budget_bytes=int(4 * 2**30)) == 2,
          "4 GiB x 0.5 / 1 GiB = 2")

    # ------------------------------------------------------------------ the planner
    plan = resources.plan_resources(district_count=1, cores=8, available_memory_gib=61.0)
    check("one district on the Kasur box runs alone",
          plan.districts_in_parallel == 1, f"{plan.districts_in_parallel}")
    check("and gets the cores, less one for the parent",
          plan.static_worker_count == 7 and plan.ndvi_worker_count == 7,
          f"ndvi={plan.ndvi_worker_count} static={plan.static_worker_count}")

    plan = resources.plan_resources(district_count=10, cores=8, available_memory_gib=61.0)
    check("ten districts on 8 cores / 61 GiB run 4 at a time",
          plan.districts_in_parallel == 4, f"{plan.districts_in_parallel}")
    check("each gets its own core share",
          plan.static_worker_count == 2, f"{plan.static_worker_count}")
    check("and its own memory budget, not the whole box",
          plan.memory_gib_per_district and plan.memory_gib_per_district < 61.0 / 3,
          f"{plan.memory_gib_per_district:.1f} GiB each")

    plan = resources.plan_resources(district_count=10, cores=4, available_memory_gib=8.0)
    check("a small laptop runs districts one at a time however many were asked for",
          plan.districts_in_parallel == 1, f"{plan.districts_in_parallel}")
    check("8 GiB is not actually tight for this workload, so the chunk stays",
          plan.static_chunk_size == 2048, f"chunk={plan.static_chunk_size}")

    plan = resources.plan_resources(district_count=4, cores=4, available_memory_gib=4.0)
    check("a genuinely tight box shrinks the chunk rather than the pool",
          plan.static_chunk_size == 1024 and plan.districts_in_parallel == 1,
          f"chunk={plan.static_chunk_size}, {plan.districts_in_parallel} district(s)")

    plan = resources.plan_resources(district_count=8, cores=64, available_memory_gib=512.0)
    check("a big server actually uses itself",
          plan.districts_in_parallel == 8 and plan.static_worker_count >= 8,
          f"{plan.districts_in_parallel} districts x {plan.static_worker_count} workers")
    check("a big server keeps the full chunk",
          plan.static_chunk_size == 2048, f"chunk={plan.static_chunk_size}")

    plan = resources.plan_resources(district_count=4, cores=8, available_memory_gib=61.0,
                                    districts_in_parallel=1, static_worker_count=3,
                                    static_chunk_size=512)
    check("every field can be overridden",
          plan.districts_in_parallel == 1 and plan.static_worker_count == 3
          and plan.static_chunk_size == 512, plan.describe()[:70])

    plan = resources.plan_resources(district_count=2, cores=8, available_memory_gib=None)
    check("unknown RAM does not crash the planner",
          plan.districts_in_parallel >= 1 and plan.static_chunk_size == 2048,
          plan.describe()[:70])

    check("the plan hands PipelineConfig exactly the fields it understands",
          set(plan.config_overrides()) == {"ndvi_worker_count", "static_worker_count",
                                           "stac_worker_count", "static_chunk_size"})
    check("cpu_count reports something usable", resources.cpu_count() >= 1,
          f"{resources.cpu_count()} core(s) visible")
