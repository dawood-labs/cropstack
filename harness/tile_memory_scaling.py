"""How does acquisition RSS scale with the number of tiles processed?

The test AOI is only 4 tiles. A district is 20-60x larger, so whether RSS grows *per
tile* or stays flat decides if the same code OOMs at production size.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")

TILE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*\[(\d+)/(\d+)\] tile (\d+): written \(([\d.]+)s")


def analyse(name):
    log = (ROOT / "logs" / f"{name}.log").read_text(errors="ignore").replace("\r", "\n")
    samples = pd.read_csv(ROOT / "metrics" / f"{name}_samples.csv")
    t0 = samples["t_epoch"].iloc[0] - samples["elapsed_s"].iloc[0]

    events = []
    for line in log.split("\n"):
        m = TILE_RE.match(line.strip())
        if m:
            stamp = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            events.append({"idx": int(m.group(2)), "total": int(m.group(3)),
                           "tile": m.group(4), "secs": float(m.group(5)),
                           "elapsed_s": stamp - t0})
    if not events:
        print(f"{name}: no tile-completion lines found")
        return

    print(f"\n=== {name}: RSS at each tile completion ===")
    print(f"{'tile#':>6}{'tile_id':>9}{'fetch_s':>9}{'elapsed_s':>11}{'rss_tree_MB':>13}{'delta_MB':>10}")
    previous = None
    for event in events:
        near = samples.iloc[(samples["elapsed_s"] - event["elapsed_s"]).abs().argsort()[:1]]
        rss = float(near["rss_tree_mb"].iloc[0])
        delta = "" if previous is None else f"{rss - previous:+.0f}"
        print(f"{event['idx']:>6}{event['tile']:>9}{event['secs']:>9.1f}"
              f"{event['elapsed_s']:>11.0f}{rss:>13.0f}{delta:>10}")
        previous = rss

    # The tiles are fetched CONCURRENTLY (farmdar.sentinel runs `workers` threads), so
    # completion timestamps cluster at the end and there is no per-tile accumulation to
    # extrapolate. What peak RSS actually tracks is how many tiles are in flight at once
    # -- min(stac_worker_count, n_tiles) -- times each tile's working set.
    peak = float(samples["rss_tree_mb"].max())
    concurrent = min(8, events[0]["total"])          # stac_worker_count default is 8
    span = events[-1]["elapsed_s"] - events[0]["elapsed_s"]
    print(f"\n  {events[0]['total']} tiles, all completing within {span:.0f} s of each other"
          f"  -> fetched concurrently, not sequentially")
    print(f"  peak tree RSS {peak/1024:.2f} GiB with ~{concurrent} tiles in flight"
          f"  =~ {peak/concurrent:.0f} MB per in-flight tile")
    print("  => peak memory scales with stac_worker_count and tile AREA, not with the")
    print("     number of tiles, so a bigger AOI does not by itself raise the peak,")
    print("     but a larger stac_tile_size_deg or more workers does.")
    for workers, deg in ((8, 0.1), (8, 0.2), (4, 0.2)):
        area_factor = (deg / 0.1) ** 2
        est = (peak / concurrent) * min(workers, 99) * area_factor
        print(f"     est. peak at stac_worker_count={workers}, tile_deg={deg}: {est/1024:5.1f} GiB")


for name in sys.argv[1:]:
    analyse(name)
