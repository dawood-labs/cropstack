"""Run one district from the command line, without the notebook.

    python run.py --crop wheat --year 2025 --district kasur \
        --aoi /data/Kasur.shp --key /keys/service-account.json --region punjab

Every `PipelineConfig` field can be overridden with `--set name=value`; values are
parsed as JSON when possible, so numbers, booleans, null and lists all work:

    python run.py ... --set stac_static_mode=manual --set 'stac_static_dates=["2025-02-15"]'
    python run.py ... --set static_window_start_at=2 --set qc_min_static_retention_pct=null

Use `batch.py` instead when running many districts in one go.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import build_pipeline_config  # noqa: E402
from pipeline import run_pipeline  # noqa: E402


def _parse_override(text: str) -> tuple:
    if "=" not in text:
        raise argparse.ArgumentTypeError(
            f"--set expects name=value, got {text!r} (e.g. --set stac_static_mode=manual)")
    name, _, raw = text.partition("=")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw          # a plain string; the common case
    return name.strip(), value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the FAO crop-mapping pipeline for one district.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--crop", required=True, help="cane | wheat | spr_maize | rice")
    parser.add_argument("--year", required=True)
    parser.add_argument("--district", required=True, help="used to name the outputs")
    parser.add_argument("--aoi", required=True, help="path to the AOI (.shp/.gpkg/.geojson/...)")
    parser.add_argument("--key", help="GEE/GCS service-account JSON")
    parser.add_argument("--out", help="output directory (default: alongside the AOI)")
    parser.add_argument("--region", help="e.g. punjab / sindh; selects region-specific windows")
    parser.add_argument("--ndvi-source", choices=("stac", "gee"), default="stac")
    parser.add_argument("--static-source", choices=("stac", "gee"), default="stac")
    parser.add_argument("--run-mode", choices=("resume", "new"), default="resume",
                        help="resume reuses finished stages; new starts a fresh run folder")
    parser.add_argument("--set", dest="overrides", action="append", type=_parse_override,
                        default=[], metavar="NAME=VALUE",
                        help="override any PipelineConfig field; repeatable")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--print-config", action="store_true",
                        help="build and print the config, then stop without running")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout, force=True)

    kwargs = {"aoi_path": args.aoi, "ndvi_source": args.ndvi_source,
              "static_source": args.static_source, "run_mode": args.run_mode}
    if args.key:
        kwargs["gee_service_account_key"] = args.key
    if args.out:
        kwargs["output_dir"] = args.out
    if args.region:
        kwargs["region"] = args.region
    kwargs.update(dict(args.overrides))

    cfg = build_pipeline_config(args.crop, args.year, args.district, **kwargs)
    print("=" * 78)
    print(cfg.summary())
    print("=" * 78, flush=True)
    if args.print_config:
        return 0

    outcome = run_pipeline(cfg)
    print("\n=== RESULT ===")
    print(json.dumps(outcome, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
