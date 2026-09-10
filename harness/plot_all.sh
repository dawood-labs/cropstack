#!/usr/bin/env bash
ROOT=/home/jovyan/FAO/optimized_code_testing
cd "$ROOT" || exit 1
for csv in metrics/*_samples.csv; do
    NAME=$(basename "$csv" _samples.csv)
    case "$NAME" in bench_*) continue;; esac
    [ -f "metrics/${NAME}_timeline.png" ] && [ "metrics/${NAME}_timeline.png" -nt "$csv" ] && continue
    ./cropstack_venv/bin/python harness/plot_timeline.py --name "$NAME" 2>/dev/null \
        || echo "  plot failed: $NAME"
done
ls -1 metrics/*_timeline.png | wc -l
