"""R2-1 on real data: make window 1's scoring fail and watch what the run does.

Windows 2 and 3 are scored against the live catalogue exactly as in a normal run; only
window 1's `select_static_dates` call is forced to raise, reproducing the Planetary
Computer rate limit that silently dropped it during RETEST_2 and shipped 8,322 acres
where a complete comparison gives 3,992.

Four cases:
  A  every attempt fails, on_score_error="error"  -> run must STOP
  B  every attempt fails, on_score_error="warn"   -> run proceeds, marked incomplete
  C  fails twice then succeeds                    -> retry wins, decision unaffected
  D  no injected failure (control)                -> the normal answer
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
REPO = ROOT / "cropstack"
sys.path.insert(0, str(REPO))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                    stream=sys.stdout, force=True)
logger = logging.getLogger("verify_r2_1")

AOI = str(ROOT / "test_aois_small/okara_test_data_spr_maize.shp")
KEY = str(ROOT / "gcs_data_downloader_ee_farmdar.json")
SEED_NDVI = ROOT / "runs_retest/P4_sprmaize_2025/1_ndvi_run_1"
WINDOW_1_START = "2025-05-01"   # spr_maize's leading window


class RateLimited(Exception):
    """Stands in for the real thing: 'You have exceeded a rate limit.'"""


def install_failure(fail_first_n: int | None):
    """Patches farmdar's selector so window 1 raises. `None` = always fail.

    Returns a callable giving the number of window-1 attempts actually made, which is how
    the retry count is verified rather than assumed.
    """
    import farmdar.sentinel as sentinel

    original = getattr(sentinel, "_verify_r2_1_original", sentinel.select_static_dates)
    sentinel._verify_r2_1_original = original
    state = {"attempts": 0}

    def patched(aoi, start, end, **kwargs):
        if str(start).startswith(WINDOW_1_START):
            state["attempts"] += 1
            if fail_first_n is None or state["attempts"] <= fail_first_n:
                raise RateLimited("You have exceeded a rate limit. "
                                  "Contact planetarycomputer@microsoft.com.")
        return original(aoi, start, end, **kwargs)

    sentinel.select_static_dates = patched
    return lambda: state["attempts"]


def restore():
    import farmdar.sentinel as sentinel
    if hasattr(sentinel, "_verify_r2_1_original"):
        sentinel.select_static_dates = sentinel._verify_r2_1_original


def seed(name: str) -> Path:
    out = ROOT / "runs_retest" / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(SEED_NDVI, out / "1_ndvi_run_1")
    return out


def case(label: str, name: str, fail_first_n, **overrides) -> dict:
    from config import build_pipeline_config
    import pipeline

    print("\n" + "=" * 78)
    print(f"### {label}")
    print("=" * 78, flush=True)

    out = seed(name)
    attempts_made = install_failure(fail_first_n) if fail_first_n is not None or fail_first_n is None else None
    record: dict = {"case": label, "run": name}
    started = time.time()
    try:
        cfg = build_pipeline_config(
            "spr_maize", "2025", "okara", aoi_path=AOI, gee_service_account_key=KEY,
            output_dir=str(out), ndvi_source="stac", static_source="stac",
            stac_static_mode="auto", run_mode="resume", **overrides)
        outcome = pipeline.run_pipeline(cfg)
        record["status"] = "completed"
        record["static_dates"] = Path(outcome["sieved_static_raster"]).parent.name
        check = outcome.get("result_check") or {}
        record["acres"] = check.get("crop_acres")
        record["features"] = check.get("feature_count")
        record["retention_pct"] = check.get("static_retention_pct")
    except BaseException as exc:  # noqa: BLE001
        record["status"] = "stopped"
        record["error_type"] = type(exc).__name__
        record["error"] = str(exc)
    finally:
        record["window_1_scoring_attempts"] = attempts_made() if attempts_made else 0
        record["seconds"] = round(time.time() - started, 1)
        restore()
    print("\n--- case result ---")
    print(json.dumps(record, indent=2, default=str), flush=True)
    return record


def main() -> int:
    results = []
    # D first, so the uncontaminated answer is on record before anything is patched.
    restore()
    results.append(case("D control - no injected failure", "V1_D_control", fail_first_n=0))
    results.append(case("A window 1 always fails, on_score_error=error (default)",
                        "V1_A_error", fail_first_n=None))
    results.append(case("B window 1 always fails, on_score_error=warn",
                        "V1_B_warn", fail_first_n=None,
                        static_window_on_score_error="warn"))
    results.append(case("C window 1 fails twice then succeeds", "V1_C_retry_recovers",
                        fail_first_n=2))

    (ROOT / "metrics" / "verify_r2_1.json").write_text(json.dumps(results, indent=2, default=str))
    print("\nwrote metrics/verify_r2_1.json")

    by = {r["case"][0]: r for r in results}
    ok = True

    def assert_(name, condition, detail=""):
        nonlocal ok
        ok = ok and bool(condition)
        print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))

    print("\n=== verdict ===")
    assert_("A: run STOPS instead of shipping window 2's acreage",
            by["A"]["status"] == "stopped", f"{by['A']['status']} {by['A'].get('error','')[:80]}")
    assert_("A: it stopped because a higher-ranked window was NOT SCORED",
            "could not be scored and rank higher" in by["A"].get("error", ""),
            by["A"].get("error", "")[:110])
    assert_("A: 3 scoring attempts were made for window 1",
            by["A"]["window_1_scoring_attempts"] == 3,
            f"{by['A']['window_1_scoring_attempts']} attempts")
    assert_("B: on_score_error='warn' proceeds",
            by["B"]["status"] == "completed", f"{by['B']['status']}")
    assert_("B: proceeding gives window 2's answer, as expected",
            by["B"].get("acres") not in (None, by["D"].get("acres")),
            f"warn={by['B'].get('acres')} control={by['D'].get('acres')}")
    assert_("C: a failure that recovers on retry does not change the decision",
            by["C"].get("acres") == by["D"].get("acres")
            and by["C"].get("static_dates") == by["D"].get("static_dates"),
            f"retry={by['C'].get('acres')} @ {by['C'].get('static_dates')} | "
            f"control={by['D'].get('acres')} @ {by['D'].get('static_dates')}")
    assert_("C: it really did retry (3 attempts: 2 failures + 1 success)",
            by["C"]["window_1_scoring_attempts"] == 3,
            f"{by['C']['window_1_scoring_attempts']} attempts")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
