"""Timeline plot per scenario: RAM, CPU, network and disk against wall clock, with
stage bands shaded so a spike can be attributed to a stage rather than just reported
as a maximum."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

STAGE_COLOURS = {
    "resolve_models": "#c7c7c7",
    "ndvi_acquire_stac": "#7fb3d5",
    "ndvi_acquire_gee": "#5499c7",
    "ndvi_stage": "#eaf2f8",
    "ndvi_mosaic": "#f5b041",
    "sieve": "#c39bd3",
    "static_stage": "#eafaf1",
    "static_acquire_stac": "#82e0aa",
    "static_acquire_gee": "#52be80",
    "static_crop_mask": "#f7dc6f",
    "static_classify": "#e59866",
    "vector_stage": "#f1948a",
}


def plot(name: str, metrics_dir: Path, out_path: Path) -> None:
    samples = pd.read_csv(metrics_dir / f"{name}_samples.csv")
    events_path = metrics_dir / f"{name}_events.json"
    events = json.loads(events_path.read_text()) if events_path.exists() else []

    fig, axes = plt.subplots(4, 1, figsize=(15, 11), sharex=True)
    t = samples["elapsed_s"] / 60.0

    axes[0].plot(t, samples["rss_tree_mb"] / 1024.0, lw=1.0, color="#c0392b", label="process-tree RSS")
    axes[0].plot(t, samples["rss_max_proc_mb"] / 1024.0, lw=0.8, color="#7d3c98",
                 label="largest single process")
    axes[0].set_ylabel("RAM (GiB)")
    axes[0].legend(loc="upper left", fontsize=8)
    axes[0].set_title(f"{name} — resource timeline (peak tree RSS "
                      f"{samples['rss_tree_mb'].max()/1024:.2f} GiB)")

    # The first tick divides a long-lived process's whole cpu_time by a tiny interval,
    # so it reports a meaningless spike; start the CPU series after it.
    warm = samples.iloc[3:] if len(samples) > 4 else samples
    t_warm = warm["elapsed_s"] / 60.0
    axes[1].plot(t_warm, warm["cpu_tree_pct"], lw=0.8, color="#1f618d", label="pipeline tree CPU %")
    axes[1].plot(t_warm, warm["cpu_system_pct"], lw=0.6, color="#95a5a6", alpha=0.7, label="system CPU %")
    axes[1].set_ylim(0, max(900, float(warm["cpu_tree_pct"].max()) * 1.1))
    axes[1].axhline(800, ls="--", lw=0.8, color="k", alpha=0.5)
    axes[1].text(0.002, 810, "8 cores saturated", fontsize=7, transform=axes[1].get_yaxis_transform())
    axes[1].set_ylabel("CPU (%)")
    axes[1].legend(loc="upper left", fontsize=8)

    axes[2].plot(t, samples["net_recv_mb"], lw=1.0, color="#148f77", label="network received (cum MB)")
    axes[2].plot(t, samples["net_sent_mb"], lw=0.8, color="#b7950b", label="network sent (cum MB)")
    axes[2].set_ylabel("Network (MB)")
    axes[2].legend(loc="upper left", fontsize=8)

    axes[3].plot(t, samples["tree_write_mb"], lw=1.0, color="#7e5109", label="tree disk write (cum MB)")
    axes[3].plot(t, samples["tree_read_mb"], lw=1.0, color="#0e6251", label="tree disk read (cum MB)")
    axes[3].plot(t, samples["outdir_mb"], lw=1.2, color="#922b21", label="output dir size (MB)")
    axes[3].set_ylabel("Disk (MB)")
    axes[3].set_xlabel("elapsed (minutes)")
    axes[3].legend(loc="upper left", fontsize=8)

    # Shade the sub-stages (skip the two umbrella stages so bands stay readable).
    umbrella = {"ndvi_stage", "static_stage", "pipeline"}
    for event in events:
        if event["stage"] in umbrella:
            continue
        colour = STAGE_COLOURS.get(event["stage"], "#d5d8dc")
        for ax in axes:
            ax.axvspan(event["start_s"] / 60.0, event["end_s"] / 60.0,
                       color=colour, alpha=0.35, lw=0)
        axes[0].annotate(event["stage"], xy=(event["start_s"] / 60.0, 1.005),
                         xycoords=("data", "axes fraction"), fontsize=6.5,
                         rotation=45, ha="left", va="bottom")

    for ax in axes:
        ax.grid(alpha=0.25, lw=0.5)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    print(f"wrote {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--metrics-dir", default="/home/jovyan/FAO/optimized_code_testing/metrics")
    args = parser.parse_args()
    metrics_dir = Path(args.metrics_dir)
    plot(args.name, metrics_dir, metrics_dir / f"{args.name}_timeline.png")


if __name__ == "__main__":
    main()
