"""Runs one cropstack scenario under full resource instrumentation.

    python run_scenario.py --name A1_cane_2025 --spec spec.json

`spec.json` holds the kwargs for `build_pipeline_config` (crop/year/district_name plus
overrides). Nothing about pipeline behaviour is changed; the harness only wraps stage
functions for timing and samples the process tree.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

REPO = Path("/home/jovyan/FAO/optimized_code_testing/cropstack")
HARNESS = Path("/home/jovyan/FAO/optimized_code_testing/harness")
METRICS_ROOT = Path("/home/jovyan/FAO/optimized_code_testing/metrics")

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HARNESS))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--spec", required=True, help="JSON file of build_pipeline_config kwargs")
    parser.add_argument("--metrics-dir", default=str(METRICS_ROOT))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    metrics_dir = Path(args.metrics_dir)
    metrics_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout, force=True,
    )

    spec = json.loads(Path(args.spec).read_text())
    os.chdir(REPO)

    from instrument import ResourceSampler, StageTimer, install_stage_wrappers

    # Build the config first: config errors are part of what we are testing, and we want
    # them recorded with a traceback rather than crashing the harness.
    result = {
        "name": args.name, "spec": spec, "status": "unknown",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    sampler = None
    try:
        from config import build_pipeline_config

        build_kwargs = dict(spec)
        crop = build_kwargs.pop("crop")
        year = build_kwargs.pop("year")
        district = build_kwargs.pop("district_name")

        cfg = build_pipeline_config(crop, year, district, **build_kwargs)
        result["config_summary"] = cfg.summary()
        print("=" * 78, flush=True)
        print(cfg.summary(), flush=True)
        print("=" * 78, flush=True)

        from pipeline import default_output_dir
        out_dir = Path(cfg.output_dir) if cfg.output_dir else default_output_dir(cfg)
        result["output_dir"] = str(out_dir)

        sampler = ResourceSampler(
            csv_path=metrics_dir / f"{args.name}_samples.csv",
            watch_dir=out_dir,
        ).start()
        timer = StageTimer(sampler)
        install_stage_wrappers(timer)

        import pipeline as pipeline_module

        sampler.mark("pipeline")
        started = time.time()
        outcome = pipeline_module.run_pipeline(cfg)
        result["status"] = "ok"
        result["outcome"] = outcome
        result["wall_s"] = round(time.time() - started, 2)

    except BaseException as exc:  # noqa: BLE001 - the point is to record everything
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        print(result["traceback"], file=sys.stderr, flush=True)
    finally:
        if sampler is not None:
            sampler.stop()
            result["resources"] = sampler.summary()
            try:
                timer.write(metrics_dir / f"{args.name}_events.json")
                result["events"] = timer.events
            except Exception:
                pass
        result["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (metrics_dir / f"{args.name}_result.json").write_text(json.dumps(result, indent=2, default=str))

    print("\n=== RESULT ===", flush=True)
    print(json.dumps({k: v for k, v in result.items() if k not in ("events", "traceback", "spec")},
                     indent=2, default=str), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
