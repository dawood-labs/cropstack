"""RETEST_2 / FAIL-12: interrupt raster_io.mosaic_geotiffs and postprocess sieve mid-write.

Runs the real function in a child process against real run data, SIGKILLs the child as
soon as it announces the write, then reports what is left at the final output path.
The pass condition is the same one the static-classify fix asserts: nothing at the final
path -- only a `.tmp.tif`, or nothing at all.
"""
from __future__ import annotations
import json, os, signal, subprocess, sys, time
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
REPO = ROOT / "cropstack"
PY = str(ROOT / "cropstack_venv" / "bin" / "python")
ENV = {**os.environ,
       "PYTHONPATH": "/home/jovyan/shared/git/standard-libraries/.worktrees/824850c677f49ef5b23af6040e9d2b165e586996",
       "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "PYTHONUNBUFFERED": "1"}

CHILD = r'''
import logging, sys, time
sys.path.insert(0, "{repo}")
logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stdout, force=True)
{body}
print("CHILD_COMPLETED_NORMALLY", flush=True)
'''

MOSAIC_BODY = '''
import raster_io
inputs = {inputs!r}
raster_io.mosaic_geotiffs(inputs, "{out}", nodata=255)
'''

SIEVE_BODY = '''
import postprocess
postprocess.apply_strict_directional_sieve("{src}", [1, 4, 5, 6, 7], min_pixel_size=20, nodata_val=255)
'''


def run_and_kill(label, body, final_path, marker, delay):
    final = Path(final_path)
    for stale in list(final.parent.glob(final.name + "*")):
        stale.unlink()
    script = CHILD.format(repo=REPO, body=body)
    proc = subprocess.Popen([PY, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=ENV, cwd=str(REPO), text=True, bufsize=1,
                            preexec_fn=os.setsid)
    seen, killed, lines = False, False, []
    started = time.time()
    while proc.poll() is None:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line.rstrip())
        if marker in line and not seen:
            seen = True
            time.sleep(delay)
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            killed = True
            break
        if time.time() - started > 600:
            break
    try:
        proc.wait(timeout=10)
    except Exception:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    leftovers = sorted(
        ({"name": p.name, "bytes": p.stat().st_size}
         for p in final.parent.glob(final.name + "*")),
        key=lambda row: row["name"],
    ) if final.parent.exists() else []
    result = {
        "case": label, "marker": marker, "marker_seen": seen, "sigkilled": killed,
        "child_exit": proc.returncode, "final_path": str(final),
        "FINAL_PATH_EXISTS": final.exists(),
        "final_bytes": final.stat().st_size if final.exists() else None,
        "leftovers_at_dir": leftovers,
        "tail": lines[-4:],
    }
    result["verdict"] = ("PASS - nothing at the final path" if killed and not final.exists()
                         else "FAIL - final path holds a partial file" if killed and final.exists()
                         else "INCONCLUSIVE - kill did not land mid-write")
    print(json.dumps(result, indent=2), flush=True)
    return result


if __name__ == "__main__":
    out = []
    tiles = sorted((ROOT / "runs_retest/K4_staging_reuse/2_static_run_1/static_staging").glob("static_10m_tile_*.tif"))
    tiles = [str(t) for t in tiles]
    scratch = ROOT / "runs_retest" / "K_writer_probe"
    scratch.mkdir(parents=True, exist_ok=True)

    if tiles:
        out.append(run_and_kill(
            "mosaic_geotiffs interrupted mid-write",
            MOSAIC_BODY.format(inputs=tiles * 12, out=scratch / "probe_mosaic.tif"),
            scratch / "probe_mosaic.tif", "Mosaicking", 0.35))
    else:
        out.append({"case": "mosaic", "verdict": "SKIPPED - no staging tiles"})

    # The sieve derives its own output path from the input, and its write is fast, so
    # give it a genuinely large input in a scratch directory: a 3x3 tiling of the real
    # spr_maize map (44.6 Mpx), which makes the write window wide enough to hit.
    import numpy as np, rasterio
    big = scratch / "probe_big.tif"
    if not big.exists():
        real = ROOT / "runs_retest/P4_sprmaize_2025/1_ndvi_run_1/okara_test_data_spr_maize_rf_classification_map.tif"
        with rasterio.open(real) as s0:
            a = s0.read(1); prof = s0.profile
        tiled = np.tile(a, (3, 3))
        prof.update(height=tiled.shape[0], width=tiled.shape[1], compress="lzw",
                    tiled=True, blockxsize=256, blockysize=256, bigtiff="YES")
        with rasterio.open(big, "w", **prof) as dst:
            dst.write(tiled, 1)
    sieved_final = scratch / f"{big.stem}_sieved_p20.tif"
    out.append(run_and_kill(
        "apply_strict_directional_sieve interrupted mid-write",
        SIEVE_BODY.format(src=big),
        sieved_final, "Writing topologically-enforced output", 0.05))

    (ROOT / "metrics" / "retest2_kill_writers.json").write_text(json.dumps(out, indent=2))
    print("\nwrote metrics/retest2_kill_writers.json")
