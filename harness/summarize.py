"""Aggregates every scenario's metrics + outputs into one table for the report."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
METRICS = ROOT / "metrics"

STAGE_ORDER = [
    "resolve_models", "ndvi_stage", "ndvi_acquire_stac", "ndvi_acquire_gee",
    "ndvi_mosaic", "static_stage", "static_acquire_stac", "static_acquire_gee",
    "static_crop_mask", "static_classify", "vector_stage", "sieve",
]


def load_samples(name):
    """(elapsed_s, rss_tree_mb) pairs for one scenario, or [] when absent."""
    path = METRICS / f"{name}_samples.csv"
    if not path.exists():
        return []
    rows = []
    with path.open() as handle:
        header = handle.readline().rstrip("\n").split(",")
        try:
            i_t, i_rss = header.index("elapsed_s"), header.index("rss_tree_mb")
        except ValueError:
            return []
        for line in handle:
            parts = line.rstrip("\n").split(",")
            if len(parts) <= max(i_t, i_rss):
                continue
            try:
                rows.append((float(parts[i_t]), float(parts[i_rss])))
            except ValueError:
                continue
    return rows


def peak_rss_in_window(samples, start_s, end_s):
    inside = [rss for t, rss in samples if start_s <= t <= end_s]
    return round(max(inside), 1) if inside else 0.0

def load_all():
    rows = []
    for result_path in sorted(METRICS.glob("*_result.json")):
        name = result_path.name[: -len("_result.json")]
        result = json.loads(result_path.read_text())
        events = result.get("events") or []
        samples = load_samples(name)
        stage_time, stage_peak = {}, {}
        for event in events:
            stage_time[event["stage"]] = stage_time.get(event["stage"], 0.0) + event["duration_s"]
            # Derive the peak from the raw timeline rather than the live counter: a stage
            # shorter than the sample interval otherwise reports 0.
            peak = peak_rss_in_window(samples, event["start_s"], event["end_s"])
            stage_peak[event["stage"]] = max(stage_peak.get(event["stage"], 0.0), peak)
        resources = result.get("resources") or {}
        outcome = result.get("outcome") or {}
        rows.append({
            "name": name,
            "status": result.get("status"),
            "error": result.get("error"),
            "wall_s": result.get("wall_s"),
            "wall_min": round(result["wall_s"] / 60, 2) if result.get("wall_s") else None,
            "output_dir": result.get("output_dir"),
            "stage_time_s": stage_time,
            "stage_peak_rss_mb": stage_peak,
            "peak_rss_tree_mb": resources.get("peak_rss_tree_mb"),
            "peak_rss_single_proc_mb": resources.get("peak_rss_single_proc_mb"),
            "peak_outdir_mb": resources.get("peak_outdir_mb"),
            "final_outdir_mb": resources.get("final_outdir_mb"),
            "net_recv_mb": resources.get("net_recv_mb"),
            "tree_read_mb": resources.get("tree_read_mb"),
            "tree_write_mb": resources.get("tree_write_mb"),
            "ndvi_minutes": outcome.get("ndvi_minutes"),
            "static_minutes": outcome.get("static_minutes"),
            "total_minutes": outcome.get("total_minutes"),
            "ndvi_run": outcome.get("ndvi_run"),
            "static_run": outcome.get("static_run"),
            "vector_run": outcome.get("vector_run"),
            "vector_output": outcome.get("vector_output"),
        })
    return rows


def main():
    rows = load_all()
    (METRICS / "summary.json").write_text(json.dumps(rows, indent=2))

    header = (f"{'scenario':<28}{'status':<9}{'wall_min':>9}{'ndvi_m':>8}{'static_m':>9}"
              f"{'peakRAM_GB':>11}{'peakDisk_MB':>12}{'net_MB':>9}")
    print(header)
    print("-" * len(header))
    for row in rows:
        peak_gb = (row["peak_rss_tree_mb"] or 0) / 1024
        print(f"{row['name']:<28}{str(row['status']):<9}"
              f"{(row['wall_min'] or 0):>9.2f}{(row['ndvi_minutes'] or 0):>8.1f}"
              f"{(row['static_minutes'] or 0):>9.1f}{peak_gb:>11.2f}"
              f"{(row['peak_outdir_mb'] or 0):>12.1f}{(row['net_recv_mb'] or 0):>9.0f}")

    print("\nper-stage seconds:")
    for row in rows:
        parts = [f"{s}={row['stage_time_s'][s]:.0f}s" for s in STAGE_ORDER
                 if s in row["stage_time_s"]]
        print(f"  {row['name']:<28} " + "  ".join(parts))
    print(f"\nwrote {METRICS/'summary.json'}")


if __name__ == "__main__":
    main()
