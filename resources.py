"""Decide how much of this machine to use, from what the machine actually is.

Every default in this pipeline was chosen on one 8-core / 61 GiB box. On a 4-core
laptop the same defaults oversubscribe; on a 64-core server they leave most of it idle,
and the memory sizing that protects the laptop then reserves tens of GiB of nothing.
Neither is something a user should have to work out per district.

`plan_resources` takes the two numbers only the machine knows (cores, free RAM) and the
one only the operator knows (how many districts), and returns a `ResourcePlan`: how many
districts to run at once, how many workers each stage gets inside one district, and the
memory budget each district may size itself against. Every field can be overridden.

Nothing here decides anything agronomic. It decides how to spend the box.
"""
from __future__ import annotations

import logging
import math
import multiprocessing
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

#: What one in-flight STAC tile costs while it is being built, at the default
#: 0.1 deg / 10 m. Scales with tile area. Measured on the Okara and Kasur runs.
STAC_TILE_GIB = 1.5
#: Floor below which parallelism is a false economy: a district needs roughly this much
#: to hold its rasters, its model and one window in flight without thrashing.
MIN_GIB_PER_DISTRICT = 6.0
#: Leave this much of RAM alone for the OS, the page cache and GDAL's own buffers.
RAM_HEADROOM_FRACTION = 0.2


def cpu_count() -> int:
    """Cores this process may actually use.

    `os.cpu_count()` reports the host's cores, not the container's. In a cgroup-limited
    pod -- which is where this pipeline runs -- that overcounts, and a pool sized from it
    is throttled rather than fast. `sched_getaffinity` is the honest number where it
    exists.
    """
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, multiprocessing.cpu_count())


def available_gib() -> Optional[float]:
    """Free RAM in GiB, or None when it cannot be determined."""
    try:
        import psutil

        return psutil.virtual_memory().available / 2**30
    except Exception:
        try:
            return (os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / 2**30
        except (ValueError, OSError, AttributeError):
            return None


@dataclass
class ResourcePlan:
    """What to spend on a run. Pass the per-district fields straight to PipelineConfig."""

    districts_in_parallel: int
    cores_total: int
    available_gib: Optional[float]
    memory_gib_per_district: Optional[float]
    ndvi_worker_count: int
    static_worker_count: int
    stac_worker_count: int
    static_chunk_size: int
    reasons: list = field(default_factory=list)

    def config_overrides(self) -> dict:
        """The subset a `PipelineConfig` understands, ready to splat into a job."""
        return {
            "ndvi_worker_count": self.ndvi_worker_count,
            "static_worker_count": self.static_worker_count,
            "stac_worker_count": self.stac_worker_count,
            "static_chunk_size": self.static_chunk_size,
        }

    def describe(self) -> str:
        ram = f"{self.available_gib:.1f} GiB free" if self.available_gib else "RAM unknown"
        per = (f"{self.memory_gib_per_district:.1f} GiB each"
               if self.memory_gib_per_district else "unbudgeted")
        return (
            f"Resource plan: {self.cores_total} core(s), {ram} -> "
            f"{self.districts_in_parallel} district(s) at a time ({per}); "
            f"per district: ndvi={self.ndvi_worker_count}, static={self.static_worker_count}, "
            f"stac={self.stac_worker_count}, chunk={self.static_chunk_size}"
            + ("  [" + "; ".join(self.reasons) + "]" if self.reasons else "")
        )


def plan_resources(
    district_count: int = 1,
    cores: Optional[int] = None,
    available_memory_gib: Optional[float] = None,
    districts_in_parallel: Optional[int] = None,
    ndvi_worker_count: Optional[int] = None,
    static_worker_count: Optional[int] = None,
    stac_worker_count: Optional[int] = None,
    static_chunk_size: Optional[int] = None,
    memory_headroom_fraction: float = RAM_HEADROOM_FRACTION,
) -> ResourcePlan:
    """Sizes a run for this machine. Any argument given is honoured as-is.

    The shape of the decision:

    * **Districts in parallel** is bounded by memory first, cores second. A district
      needs `MIN_GIB_PER_DISTRICT` before it is worth starting at all, and at least two
      cores, so a small box runs them one at a time however many were asked for. Running
      two districts in 4 GiB is slower than running them in sequence, not faster.
    * **Within a district**, cores are split evenly between the districts running at
      once. NDVI inference and static classification never overlap -- they are separate
      stages -- so both may have the district's whole share.
    * **STAC acquisition** is I/O bound but holds each in-flight tile in memory, so it is
      capped by memory rather than by cores.
    * **`static_chunk_size`** shrinks when memory per worker is tight. It is the cheapest
      lever there is: halving it quarters the per-worker window cost and costs only a
      little more per-window overhead.
    """
    cores = max(1, cores if cores else cpu_count())
    free_gib = available_memory_gib if available_memory_gib is not None else available_gib()
    reasons = []

    # ---------------------------------------------------------- districts in parallel
    if districts_in_parallel is not None:
        parallel = max(1, districts_in_parallel)
        reasons.append(f"districts_in_parallel={parallel} (given)")
    else:
        parallel = max(1, district_count)
        if free_gib:
            by_memory = max(1, int((free_gib * (1 - memory_headroom_fraction)) // MIN_GIB_PER_DISTRICT))
            if by_memory < parallel:
                parallel = by_memory
                reasons.append(f"memory allows {by_memory}")
        by_cores = max(1, cores // 2)      # a district with one core is not parallel work
        if by_cores < parallel:
            parallel = by_cores
            reasons.append(f"cores allow {by_cores}")
        if parallel > district_count:
            parallel = max(1, district_count)

    # ------------------------------------------------------------- per-district share
    cores_each = max(1, cores // parallel)
    memory_each = (free_gib * (1 - memory_headroom_fraction) / parallel) if free_gib else None

    # NDVI inference and static classification are separate stages, so each may use the
    # district's whole core share. One core is left to the parent on a single district.
    share = cores_each if parallel > 1 else max(1, cores_each - 1)
    ndvi = ndvi_worker_count if ndvi_worker_count else share
    static = static_worker_count if static_worker_count else share

    # Acquisition is bounded by the memory its in-flight tiles need, not by cores.
    if stac_worker_count:
        stac = stac_worker_count
    elif memory_each:
        stac = max(1, min(cores_each * 2, int(memory_each // STAC_TILE_GIB)))
    else:
        stac = cores_each

    # The cheapest memory lever: shrink the window before shrinking the pool.
    if static_chunk_size:
        chunk = static_chunk_size
    elif memory_each and memory_each < MIN_GIB_PER_DISTRICT:
        # Below the floor a district needs at all, so cutting the pool further would
        # mean one worker and still be tight. Quartering the window is the cheaper move.
        chunk = 1024
        reasons.append(f"chunk 1024 ({memory_each:.1f} GiB is under the "
                       f"{MIN_GIB_PER_DISTRICT:g} GiB per-district floor)")
    else:
        chunk = 2048

    plan = ResourcePlan(
        districts_in_parallel=parallel, cores_total=cores, available_gib=free_gib,
        memory_gib_per_district=memory_each, ndvi_worker_count=ndvi,
        static_worker_count=static, stac_worker_count=stac,
        static_chunk_size=chunk, reasons=reasons,
    )
    logger.info(plan.describe())
    return plan
