"""RETEST_2 / FAIL-12 control: run the PRE-FIX static_classify against the same inputs
and interrupt it at the same point, to show the fix is what changed the outcome.

The old `static_classify.py` is taken straight out of git (c2f954d) and placed ahead of
the repo on sys.path, so the child imports the old classifier and the new everything
else -- the fix is self-contained in `classify_static_image`.
"""
from __future__ import annotations
import json, os, signal, subprocess, sys, time
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
REPO = ROOT / "cropstack"
SCRATCH = Path("/tmp/claude-1000/-home-jovyan-FAO-optimized-code-testing/68c82c36-204c-4a0f-830b-ded51d99b1dc/scratchpad")
PY = str(ROOT / "cropstack_venv" / "bin" / "python")
ENV = {**os.environ,
       "PYTHONPATH": "/home/jovyan/shared/git/standard-libraries/.worktrees/824850c677f49ef5b23af6040e9d2b165e586996",
       "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "PYTHONUNBUFFERED": "1"}

STAGING = ROOT / "runs_retest/K4_staging_reuse/2_static_run_1/static_staging/static.vrt"
NDVI = ROOT / "runs_retest/K4_staging_reuse/1_ndvi_run_1/okara_test_data_spr_maize_rf_classification_map_sieved_p20.tif"
AOI = ROOT / "test_aois_small/okara_test_data_spr_maize.shp"
MODEL = Path.home() / ".cache/fao_pipeline/models/farmdar_data_catalog/FAO_SPR_MAIZE_MODELS/FAO_Spr_Maize_Static_IMG_Model/FAO_Spr_Maize_XGB_Static_IMG_Model.json"

BODY = '''
import logging, sys, os
sys.path = [d for d in sys.path if d not in ("", os.getcwd())]
sys.path.insert(0, "{repo}")
{shadow}
logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stdout, force=True)
import static_classify
print("USING", static_classify.__file__, flush=True)
static_classify.classify_static_image(
    static_image_path="{img}", output_path="{out}",
    ndvi_classification_path="{ndvi}", aoi_path="{aoi}", model_path="{model}",
    crop_classes=(1, 4, 5, 6, 7), use_mask=True, model_positive_class=1,
    crop_label=1, background_label=8, worker_count=4)
print("CHILD_COMPLETED_NORMALLY", flush=True)
'''


def trial(label, shadow_dir, out_path):
    out = Path(out_path)
    for stale in list(out.parent.glob(out.name + "*")):
        stale.unlink()
    shadow = f'sys.path.insert(0, "{shadow_dir}")' if shadow_dir else "pass"
    script = BODY.format(shadow=shadow, repo=REPO, img=STAGING, out=out,
                         ndvi=NDVI, aoi=AOI, model=MODEL)
    proc = subprocess.Popen([PY, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=ENV, cwd=str(REPO), text=True, bufsize=1, preexec_fn=os.setsid)
    killed, lines = False, []
    while proc.poll() is None:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip())
        if "Classifying static image:  25%" in line or "25%|" in line:
            time.sleep(3)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            killed = True
            break
    try:
        proc.wait(timeout=10)
    except Exception:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    leftovers = sorted(({"name": p.name, "bytes": p.stat().st_size}
                        for p in out.parent.glob(out.name + "*")), key=lambda r: r["name"])
    rec = {"case": label, "sigkilled": killed, "child_exit": proc.returncode,
           "FINAL_PATH_EXISTS": out.exists(),
           "final_bytes": out.stat().st_size if out.exists() else None,
           "leftovers": leftovers,
           "impl": next((l for l in lines if l.startswith("USING")), None)}

    # Now: would a resume be able to read what was left at the final path?
    if out.exists():
        probe = subprocess.run([PY, "-c",
            f'import rasterio;\nsrc=rasterio.open("{out}");print("READABLE", src.shape)'],
            env=ENV, capture_output=True, text=True)
        rec["resume_read"] = ("ok: " + probe.stdout.strip()) if probe.returncode == 0 else \
                             "RAISED: " + probe.stderr.strip().splitlines()[-1]
    else:
        rec["resume_read"] = "n/a - no file at the final path, a resume recomputes"
    print(json.dumps(rec, indent=2), flush=True)
    return rec


if __name__ == "__main__":
    SCRATCH.mkdir(parents=True, exist_ok=True)
    old_dir = SCRATCH / "prefix_c2f954d"
    old_dir.mkdir(exist_ok=True)
    src = subprocess.run(["git", "show", "c2f954d:static_classify.py"], cwd=str(REPO),
                         capture_output=True, text=True, check=True).stdout
    (old_dir / "static_classify.py").write_text(src)

    results = [
        trial("PRE-FIX c2f954d (writes straight to the final path)", old_dir,
              SCRATCH / "ctl_old" / "static_mosaic_CTL_Cls.tif"),
        trial("POST-FIX ce37be8 (tmp + os.replace)", None,
              SCRATCH / "ctl_new" / "static_mosaic_CTL_Cls.tif"),
    ]
    (ROOT / "metrics" / "retest2_prefix_control.json").write_text(json.dumps(results, indent=2))
    print("\nwrote metrics/retest2_prefix_control.json")
