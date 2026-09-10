"""Emits the markdown tables used by TEST_REPORT.md / BOTTLENECKS.md."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
METRICS = ROOT / "metrics"

SUBSTAGES = ["ndvi_acquire_stac", "ndvi_acquire_gee", "ndvi_mosaic",
             "static_acquire_stac", "static_acquire_gee", "static_crop_mask",
             "static_classify", "sieve", "vector_stage", "resolve_models"]


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def scenario_table():
    rows = load(METRICS / "summary.json", [])
    validation = {r["scenario"]: r for r in load(METRICS / "final_validation.json", [])}
    out = ["| scenario | status | wall (min) | NDVI (min) | static (min) | peak RAM (GiB) "
           "| peak disk (MB) | net in (MB) | features | acres | verdict |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        v = validation.get(row["name"], {})
        vec = v.get("vector") or {}
        problems = v.get("problems") or []
        warnings = v.get("warnings") or []
        if row["status"] != "ok":
            verdict = "**RUN FAILED**"
        elif problems:
            verdict = "**FAIL** — " + "; ".join(problems)
        elif warnings:
            verdict = "PASS (warn) — " + "; ".join(warnings)
        else:
            verdict = "PASS"
        out.append(
            f"| `{row['name']}` | {row['status']} | {row['wall_min'] or 0:.2f} "
            f"| {row['ndvi_minutes'] if row['ndvi_minutes'] is not None else '—'} "
            f"| {row['static_minutes'] if row['static_minutes'] is not None else '—'} "
            f"| {(row['peak_rss_tree_mb'] or 0)/1024:.2f} "
            f"| {row['peak_outdir_mb'] or 0:.0f} "
            f"| {row['net_recv_mb'] or 0:.0f} "
            f"| {vec.get('features', '—')} "
            f"| {vec.get('total_acres', '—')} | {verdict} |")
    return "\n".join(out)


def stage_table():
    rows = load(METRICS / "summary.json", [])
    present = [s for s in SUBSTAGES if any(s in r["stage_time_s"] for r in rows)]
    out = ["| scenario | " + " | ".join(f"`{s}`" for s in present) + " |",
           "|---" * (len(present) + 1) + "|"]
    for row in rows:
        cells = []
        for stage in present:
            seconds = row["stage_time_s"].get(stage)
            cells.append(f"{seconds:.0f}s" if seconds is not None else "—")
        out.append(f"| `{row['name']}` | " + " | ".join(cells) + " |")
    return "\n".join(out)


def peak_table():
    rows = load(METRICS / "summary.json", [])
    present = [s for s in SUBSTAGES if any(s in r["stage_peak_rss_mb"] for r in rows)]
    out = ["| scenario | " + " | ".join(f"`{s}`" for s in present) + " |",
           "|---" * (len(present) + 1) + "|"]
    for row in rows:
        cells = []
        for stage in present:
            mb = row["stage_peak_rss_mb"].get(stage)
            cells.append(f"{mb/1024:.2f}" if mb else "—")
        out.append(f"| `{row['name']}` | " + " | ".join(cells) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    tables = {
        "scenarios": scenario_table(),
        "stage_seconds": stage_table(),
        "stage_peak_rss_gib": peak_table(),
    }
    (METRICS / "tables.md").write_text(
        "\n\n".join(f"### {k}\n\n{v}" for k, v in tables.items()))
    print(tables["scenarios"])
    print()
    print(tables["stage_seconds"])
