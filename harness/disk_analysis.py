"""Do delete_raw_ndvi_tiles / delete_raw_static_tiles actually reclaim the space?"""
import json
from pathlib import Path

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")
METRICS = ROOT / "metrics"


def dir_mb(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


print(f"{'scenario':<30}{'peak_MB':>10}{'now_MB':>10}{'reclaimed':>11}{'raw dirs left':>15}")
print("-" * 78)
for result_path in sorted(METRICS.glob("*_result.json")):
    name = result_path.name[: -len("_result.json")]
    result = json.loads(result_path.read_text())
    res = result.get("resources") or {}
    peak, final = res.get("peak_outdir_mb"), res.get("final_outdir_mb")
    out_dir = Path(result.get("output_dir") or ".")
    leftovers = []
    if out_dir.exists():
        for pattern in ("raw_ndvi_tiles", "static_staging", "tile_predictions"):
            for d in out_dir.rglob(pattern):
                if d.is_dir():
                    leftovers.append(f"{d.relative_to(out_dir)}={dir_mb(d):.0f}MB")
    if peak is None:
        continue
    # `final_outdir_mb` is the last 5-second sample, which for a run that ends right
    # after cleanup is taken BEFORE the rmtree lands. Measure the directory now instead.
    now_mb = dir_mb(out_dir) if out_dir.exists() else 0.0
    pct = 100 * (peak - now_mb) / peak if peak else 0
    print(f"{name:<30}{peak:>10.0f}{now_mb:>10.1f}{pct:>10.0f}%  "
          f"{', '.join(leftovers) if leftovers else 'none'}")

print("\ncurrent on-disk size of every run workspace:")
runs = ROOT / "runs"
total = 0
for d in sorted(runs.iterdir()) if runs.exists() else []:
    if d.is_dir():
        mb = dir_mb(d)
        total += mb
        print(f"  {d.name:<34}{mb:>10.1f} MB")
print(f"  {'TOTAL':<34}{total:>10.1f} MB")
