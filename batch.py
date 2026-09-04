"""Batch runner: process many districts / crops / years in one go.

Two ways in:

    # from a notebook or another script
    from batch import build_jobs, run_batch
    jobs = build_jobs(crop="cane", year="2025",
                      districts=["Muzaffargarh", "Rahimyar Khan", "Bahawalpur"],
                      ndvi_source="stac", static_source="stac")
    results = run_batch(jobs)

    # from a shell
    python batch.py --jobs jobs.json --results batch_results.csv

`jobs.json` is a list of objects, each needing `crop`, `year` and `district_name`, plus
any PipelineConfig override:

    [
      {"crop": "cane", "year": "2025", "district_name": "Muzaffargarh"},
      {"crop": "wheat", "year": "2021", "district_name": "Attock",
       "static_source": "gee", "gee_static_mode": "manual_gcs_link",
       "gee_static_gcs_uri": "gs://farmdar_data_catalog/.../Attock_2021-Jan-25.tif"}
    ]

Jobs run sequentially -- each one already saturates the machine with its own worker
pool, so running two at once would just contend for RAM. A failing job is logged and
skipped rather than killing the batch, and Earth Engine is initialised once and shared
across jobs that need it.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import resources
from config import build_pipeline_config
from pipeline import run_pipeline

logger = logging.getLogger(__name__)


PLACEHOLDERS = ("district_name", "district", "crop", "year")


def _fill_placeholders(value: Any, fields: Dict[str, str]) -> Any:
    """Substitutes `{district_name}` / `{crop}` / `{year}` in a string override.

    Plain replacement rather than `str.format`, so Windows paths and any other stray
    braces pass through untouched.
    """
    if not isinstance(value, str):
        return value
    for token, replacement in fields.items():
        value = value.replace("{" + token + "}", replacement)
    return value


def build_jobs(
    crop: str,
    year: str,
    districts: Sequence[str],
    aoi_paths: Optional[Sequence[str]] = None,
    **shared_overrides: Any,
) -> List[Dict[str, Any]]:
    """Expands one crop/year across many districts, applying the same overrides to each.

    String overrides may contain `{district_name}`, `{crop}` or `{year}` placeholders,
    filled in per district. Use a plain (non-f) string so the placeholder survives::

        build_jobs(
            crop="cane", year="2025",
            districts=["aoi_0", "aoi_1"],
            aoi_path=r"C:\\data\\split_aoi_folders\\{district_name}\\{district_name}.shp",
        )

    Pass `aoi_paths` instead when the AOIs follow no pattern -- one path per district,
    in the same order.
    """
    districts = list(districts)
    if aoi_paths is not None and len(aoi_paths) != len(districts):
        raise ValueError(
            f"aoi_paths has {len(aoi_paths)} entries but there are {len(districts)} districts."
        )

    jobs = []
    for index, district in enumerate(districts):
        fields = {
            "district_name": district, "district": district,
            "crop": crop, "year": str(year),
        }
        job: Dict[str, Any] = {"crop": crop, "year": str(year), "district_name": district}
        for key, value in shared_overrides.items():
            job[key] = _fill_placeholders(value, fields)
        if aoi_paths is not None:
            job["aoi_path"] = aoi_paths[index]
        jobs.append(job)
    return jobs


def load_jobs(jobs_path: str) -> List[Dict[str, Any]]:
    jobs = json.loads(Path(jobs_path).read_text())
    if not isinstance(jobs, list):
        raise ValueError(f"{jobs_path} must contain a JSON list of job objects.")
    for index, job in enumerate(jobs):
        missing = [key for key in ("crop", "year", "district_name") if key not in job]
        if missing:
            raise ValueError(f"Job {index} is missing required key(s): {missing}")
    return jobs


def run_batch(
    jobs: Iterable[Dict[str, Any]],
    continue_on_error: bool = True,
    results_csv: Optional[str] = None,
    plan: Optional["resources.ResourcePlan"] = None,
    auto_resources: bool = True,
) -> List[Dict[str, Any]]:
    """Runs every job, returning one result row per job (status, outputs, timings).

    Earth Engine is initialised at most once for the whole batch and reused by every
    job that needs it.

    Worker counts are sized for this machine unless the job sets them: `plan` (or, by
    default, `resources.plan_resources` given the job count) supplies `ndvi_worker_count`,
    `static_worker_count`, `stac_worker_count` and `static_chunk_size`. Anything a job
    states explicitly always wins -- the plan only fills what was left unsaid, because a
    default chosen on one box is not a default.
    """
    jobs = list(jobs)
    results: List[Dict[str, Any]] = []
    gee_credentials, gee_project = None, None

    if plan is None and auto_resources:
        plan = resources.plan_resources(district_count=len(jobs))
    defaults = plan.config_overrides() if plan else {}

    logger.info(f"Starting batch of {len(jobs)} job(s).")

    for index, job in enumerate(jobs, start=1):
        job = dict(job)
        crop, year, district = job.pop("crop"), str(job.pop("year")), job.pop("district_name")
        aoi_path = job.pop("aoi_path", None) or job.pop("aoi_shapefile", None)
        label = f"[{index}/{len(jobs)}] {crop} {year} {district}"
        logger.info(f"{label}: starting")

        started_at = time.time()
        try:
            for name, value in defaults.items():
                job.setdefault(name, value)
            cfg = build_pipeline_config(crop, year, district, aoi_path=aoi_path, **job)

            if cfg.needs_gee_api and gee_credentials is None:
                import gee_client

                gee_credentials, gee_project = gee_client.init_gee_and_gcs(
                    cfg.gee_project_name, cfg.gee_service_account_key,
                )

            outcome = run_pipeline(cfg, gee_credentials=gee_credentials, gee_project=gee_project)
            results.append({"status": "ok", "error": None, **outcome})
            logger.info(f"{label}: done in {outcome['total_minutes']:.1f} min")

        except Exception as exc:
            elapsed_minutes = round((time.time() - started_at) / 60, 1)
            logger.error(f"{label}: FAILED after {elapsed_minutes} min: {exc}")
            logger.debug(traceback.format_exc())
            results.append({
                "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                "crop": crop, "year": year, "district": district,
                "total_minutes": elapsed_minutes,
            })
            if not continue_on_error:
                break

    succeeded = sum(1 for row in results if row["status"] == "ok")
    logger.info(f"Batch finished: {succeeded} succeeded, {len(results) - succeeded} failed.")
    for row in results:
        if row["status"] == "failed":
            logger.warning(f"  FAILED: {row['crop']} {row['year']} {row['district']} -- {row['error']}")

    if results_csv:
        _write_results_csv(results, results_csv)

    return results


def _write_results_csv(results: List[Dict[str, Any]], results_csv: str) -> None:
    try:
        import pandas as pd

        pd.DataFrame(results).to_csv(results_csv, index=False)
    except ImportError:  # pandas should always be present, but a summary is not worth a crash
        import csv

        fieldnames = sorted({key for row in results for key in row})
        with open(results_csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
    logger.info(f"Batch summary written to {results_csv}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FAO pipeline over many AOIs.")
    parser.add_argument("--jobs", required=True, help="Path to a JSON list of job objects.")
    parser.add_argument("--results", default=None, help="Optional path for the summary CSV.")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="Abort the batch on the first failure (default: keep going).")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    results = run_batch(
        load_jobs(args.jobs),
        continue_on_error=not args.stop_on_error,
        results_csv=args.results,
    )
    raise SystemExit(0 if all(row["status"] == "ok" for row in results) else 1)


if __name__ == "__main__":
    main()
