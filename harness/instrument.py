"""Non-invasive instrumentation for cropstack pipeline runs.

Samples the whole process tree (parent + spawned workers) at >=1 Hz and records
per-stage wall clock, peak RSS, CPU, disk IO and network bytes. Nothing here changes
pipeline behaviour: the stage wrappers call straight through to the original function.
"""
from __future__ import annotations

import csv
import functools
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import psutil

SAMPLE_INTERVAL_S = 0.5          # 2 Hz
DISK_USAGE_EVERY_N_TICKS = 10    # du of the output dir every ~5 s


def _dir_size_bytes(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


class ResourceSampler:
    """Background sampler over the process tree. Writes one CSV row per tick."""

    FIELDS = [
        "t_epoch", "elapsed_s", "stage",
        "n_procs", "rss_tree_mb", "rss_max_proc_mb",
        "cpu_tree_pct", "cpu_system_pct",
        "mem_system_used_mb", "mem_system_available_mb",
        "tree_read_mb", "tree_write_mb",
        "sys_read_mb", "sys_write_mb",
        "net_recv_mb", "net_sent_mb",
        "outdir_mb", "fs_used_gb", "fs_free_gb",
    ]

    def __init__(self, csv_path: Path, watch_dir: Optional[Path] = None,
                 fs_path: str = "/home/jovyan"):
        self.csv_path = Path(csv_path)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.watch_dir = Path(watch_dir) if watch_dir else None
        self.fs_path = fs_path

        self.stage = "init"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self.root = psutil.Process(os.getpid())
        self.t0 = time.time()

        # Per-pid last-seen cumulative counters, so a worker that exits still counts.
        self._cpu_by_pid: Dict[int, float] = {}
        self._io_read_by_pid: Dict[int, int] = {}
        self._io_write_by_pid: Dict[int, int] = {}

        net0 = psutil.net_io_counters()
        self._net0_recv, self._net0_sent = net0.bytes_recv, net0.bytes_sent
        disk0 = psutil.disk_io_counters()
        self._disk0_read = disk0.read_bytes if disk0 else 0
        self._disk0_write = disk0.write_bytes if disk0 else 0

        self.peak_rss_tree_mb = 0.0
        self.peak_rss_proc_mb = 0.0
        self.peak_outdir_mb = 0.0
        self.samples = 0
        self.rows: List[Dict[str, Any]] = []
        # peak RSS observed while each stage label was active
        self.stage_peak_rss: Dict[str, float] = {}
        self.stage_peak_outdir: Dict[str, float] = {}

        self._last_cpu_total = 0.0
        self._last_t = self.t0
        self._outdir_mb = 0.0
        self._tick = 0

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "ResourceSampler":
        self._handle = open(self.csv_path, "w", newline="")
        self._writer = csv.DictWriter(self._handle, fieldnames=self.FIELDS)
        self._writer.writeheader()
        psutil.cpu_percent(None)  # prime the system-wide counter
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        try:
            self._handle.close()
        except Exception:
            pass

    def mark(self, stage: str) -> None:
        with self._lock:
            self.stage = stage

    # ------------------------------------------------------------------ sampling
    def _tree_procs(self) -> List[psutil.Process]:
        procs = [self.root]
        try:
            procs.extend(self.root.children(recursive=True))
        except psutil.Error:
            pass
        return procs

    def _sample(self) -> Dict[str, Any]:
        now = time.time()
        rss_total = 0
        rss_max = 0
        n = 0
        for proc in self._tree_procs():
            try:
                with proc.oneshot():
                    rss = proc.memory_info().rss
                    rss_total += rss
                    rss_max = max(rss_max, rss)
                    n += 1
                    times = proc.cpu_times()
                    self._cpu_by_pid[proc.pid] = times.user + times.system
                    try:
                        io = proc.io_counters()
                        self._io_read_by_pid[proc.pid] = io.read_bytes
                        self._io_write_by_pid[proc.pid] = io.write_bytes
                    except (psutil.AccessDenied, AttributeError):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        cpu_total = sum(self._cpu_by_pid.values())
        dt = max(now - self._last_t, 1e-6)
        if self._tick == 0:
            # First tick has no previous total to difference against; report 0 rather
            # than the process's whole lifetime CPU divided by a tiny interval.
            cpu_tree_pct = 0.0
        else:
            cpu_tree_pct = max(0.0, (cpu_total - self._last_cpu_total) / dt * 100.0)
        self._last_cpu_total, self._last_t = cpu_total, now

        virtual = psutil.virtual_memory()
        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()

        self._tick += 1
        if self.watch_dir is not None and (self._tick % DISK_USAGE_EVERY_N_TICKS == 1):
            self._outdir_mb = _dir_size_bytes(self.watch_dir) / 1e6

        usage = psutil.disk_usage(self.fs_path)
        rss_tree_mb = rss_total / 1e6
        rss_proc_mb = rss_max / 1e6

        with self._lock:
            stage = self.stage
        self.peak_rss_tree_mb = max(self.peak_rss_tree_mb, rss_tree_mb)
        self.peak_rss_proc_mb = max(self.peak_rss_proc_mb, rss_proc_mb)
        self.peak_outdir_mb = max(self.peak_outdir_mb, self._outdir_mb)
        self.stage_peak_rss[stage] = max(self.stage_peak_rss.get(stage, 0.0), rss_tree_mb)
        self.stage_peak_outdir[stage] = max(self.stage_peak_outdir.get(stage, 0.0), self._outdir_mb)

        return {
            "t_epoch": round(now, 3),
            "elapsed_s": round(now - self.t0, 2),
            "stage": stage,
            "n_procs": n,
            "rss_tree_mb": round(rss_tree_mb, 1),
            "rss_max_proc_mb": round(rss_proc_mb, 1),
            "cpu_tree_pct": round(cpu_tree_pct, 1),
            "cpu_system_pct": psutil.cpu_percent(None),
            "mem_system_used_mb": round(virtual.used / 1e6, 1),
            "mem_system_available_mb": round(virtual.available / 1e6, 1),
            "tree_read_mb": round(sum(self._io_read_by_pid.values()) / 1e6, 2),
            "tree_write_mb": round(sum(self._io_write_by_pid.values()) / 1e6, 2),
            "sys_read_mb": round(((disk.read_bytes if disk else 0) - self._disk0_read) / 1e6, 2),
            "sys_write_mb": round(((disk.write_bytes if disk else 0) - self._disk0_write) / 1e6, 2),
            "net_recv_mb": round((net.bytes_recv - self._net0_recv) / 1e6, 2),
            "net_sent_mb": round((net.bytes_sent - self._net0_sent) / 1e6, 2),
            "outdir_mb": round(self._outdir_mb, 1),
            "fs_used_gb": round(usage.used / 1e9, 2),
            "fs_free_gb": round(usage.free / 1e9, 2),
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                row = self._sample()
                self.rows.append(row)
                self._writer.writerow(row)
                self._handle.flush()
                self.samples += 1
            except Exception:
                pass
            self._stop.wait(SAMPLE_INTERVAL_S)

    # ------------------------------------------------------------------ summary
    def summary(self) -> Dict[str, Any]:
        last = self.rows[-1] if self.rows else {}
        return {
            "samples": self.samples,
            "duration_s": round(time.time() - self.t0, 1),
            "peak_rss_tree_mb": round(self.peak_rss_tree_mb, 1),
            "peak_rss_single_proc_mb": round(self.peak_rss_proc_mb, 1),
            "peak_outdir_mb": round(self.peak_outdir_mb, 1),
            "final_outdir_mb": last.get("outdir_mb"),
            "tree_read_mb": last.get("tree_read_mb"),
            "tree_write_mb": last.get("tree_write_mb"),
            "sys_read_mb": last.get("sys_read_mb"),
            "sys_write_mb": last.get("sys_write_mb"),
            "net_recv_mb": last.get("net_recv_mb"),
            "net_sent_mb": last.get("net_sent_mb"),
            "stage_peak_rss_mb": {k: round(v, 1) for k, v in self.stage_peak_rss.items()},
        }


class StageTimer:
    """Records stage/phase start-end events alongside the sampler's timeline."""

    def __init__(self, sampler: ResourceSampler):
        self.sampler = sampler
        self.events: List[Dict[str, Any]] = []
        self._depth = 0

    def wrap(self, module: Any, attribute: str, label: str) -> None:
        """Replaces `module.attribute` with a timing wrapper that calls through."""
        original = getattr(module, attribute)

        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            return self._timed(label, original, *args, **kwargs)

        wrapper.__wrapped_original__ = original
        setattr(module, attribute, wrapper)

    def _timed(self, label: str, fn: Callable, *args, **kwargs):
        parent_stage = self.sampler.stage
        self.sampler.mark(label)
        started = time.time()
        rss_at_start = self.sampler.peak_rss_tree_mb
        self.sampler.stage_peak_rss[label] = 0.0
        error = None
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            ended = time.time()
            self.events.append({
                "stage": label,
                "t_start_epoch": round(started, 3),
                "t_end_epoch": round(ended, 3),
                "start_s": round(started - self.sampler.t0, 2),
                "end_s": round(ended - self.sampler.t0, 2),
                "duration_s": round(ended - started, 2),
                "peak_rss_mb_during": round(self.sampler.stage_peak_rss.get(label, 0.0), 1),
                "error": error,
            })
            self.sampler.mark(parent_stage)

    def write(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.events, indent=2))


def install_stage_wrappers(timer: StageTimer) -> None:
    """Wraps every meaningful pipeline stage boundary. Import-order sensitive: the
    pipeline modules bind some of these by value, so patch the *binding site*."""
    import ndvi_pipeline
    import pipeline
    import postprocess
    import static_classify
    import static_pipeline

    # top-level stages (pipeline.py binds these names at import)
    timer.wrap(pipeline, "run_ndvi_pipeline", "ndvi_stage")
    timer.wrap(pipeline, "run_static_pipeline", "static_stage")
    timer.wrap(postprocess, "vectorize_process_and_export", "vector_stage")

    # NDVI sub-phases
    timer.wrap(ndvi_pipeline, "_acquire_tiles_from_stac", "ndvi_acquire_stac")
    timer.wrap(ndvi_pipeline, "_acquire_tiles_from_gee", "ndvi_acquire_gee")
    timer.wrap(ndvi_pipeline, "mosaic_prediction_tiles", "ndvi_mosaic")

    # static sub-phases
    timer.wrap(static_pipeline, "_acquire_static_from_stac", "static_acquire_stac")
    timer.wrap(static_pipeline, "_acquire_static_from_gee", "static_acquire_gee")
    timer.wrap(static_classify, "build_crop_mask", "static_crop_mask")
    timer.wrap(static_classify, "classify_static_image", "static_classify")

    # sieve is called from both stages; wrap at its definition site
    timer.wrap(postprocess, "apply_strict_directional_sieve", "sieve")
    # ndvi_pipeline / static_pipeline call postprocess.apply_... by attribute, so the
    # patch above is picked up automatically.
    import model_registry
    timer.wrap(model_registry, "resolve_pipeline_models", "resolve_models")
