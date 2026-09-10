"""Is RSS growing monotonically within a stage (leak-shaped), or is it sawtooth?"""
import json
import sys
from pathlib import Path

import pandas as pd

METRICS = Path("/home/jovyan/FAO/optimized_code_testing/metrics")


def analyse(name):
    samples = pd.read_csv(METRICS / f"{name}_samples.csv")
    # Tick 0 divides a process's whole lifetime CPU by a tiny interval; drop it.
    samples = samples[samples["elapsed_s"] > 1.5].reset_index(drop=True)
    events = json.loads((METRICS / f"{name}_events.json").read_text())
    print(f"\n=== {name} ===")
    print(f"{'stage':<24}{'dur_s':>8}{'rss_start':>11}{'rss_end':>10}{'rss_peak':>10}"
          f"{'growth':>9}{'monotonic':>11}{'cpu_mean':>10}{'cpu_max':>9}")
    for event in events:
        window = samples[(samples["elapsed_s"] >= event["start_s"])
                         & (samples["elapsed_s"] <= event["end_s"])]
        if len(window) < 3:
            continue
        rss = window["rss_tree_mb"].to_numpy()
        # Fraction of consecutive samples that do not decrease: ~1.0 means it only ever
        # climbs, which is the leak signature.
        nondecreasing = float((rss[1:] >= rss[:-1]).mean())
        cpu = window["cpu_tree_pct"]
        print(f"{event['stage']:<24}{event['duration_s']:>8.0f}{rss[0]:>11.0f}{rss[-1]:>10.0f}"
              f"{rss.max():>10.0f}{rss[-1]-rss[0]:>+9.0f}{nondecreasing:>11.2f}"
              f"{cpu.mean():>10.0f}{cpu.max():>9.0f}")

    # Low-activity stalls: little CPU and little network movement.
    net = samples["net_recv_mb"].diff().fillna(0)
    stalled = samples[(samples["cpu_tree_pct"] < 30) & (net < 1.0)]
    print(f"\nstalled samples (tree CPU <30% and <1 MB/s network): "
          f"{len(stalled)} of {len(samples)} ({100*len(stalled)/len(samples):.1f}%)")
    if len(stalled):
        by_stage = stalled.groupby("stage").size().sort_values(ascending=False)
        for stage, count in by_stage.items():
            print(f"    {stage:<26} {count:>5} samples ~ {count*0.5:>6.0f} s")


for name in sys.argv[1:]:
    analyse(name)
