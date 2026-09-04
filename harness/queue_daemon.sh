#!/usr/bin/env bash
# Single consumer of logs/pending_queue.txt. Pops the top line, runs it, repeats.
# Exits once the file is empty, so appending more work restarts nothing by accident.
ROOT=/home/jovyan/FAO/optimized_code_testing
PENDING="$ROOT/logs/pending_queue.txt"
QLOG="$ROOT/logs/queue.log"
cd "$ROOT" || exit 1

# Never run alongside the older queue scripts.
while pgrep -f "harness/queue.sh" >/dev/null || pgrep -f "harness/chain_queue.sh" >/dev/null; do
    sleep 15
done

touch "$PENDING"
echo "### daemon start $(date -Is)" >> "$QLOG"
while true; do
    NAME=$(head -n 1 "$PENDING")
    if [ -z "$NAME" ]; then
        echo "### daemon idle-exit $(date -Is)" >> "$QLOG"
        break
    fi
    sed -i '1d' "$PENDING"
    SPEC="$ROOT/specs/${NAME}.json"
    if [ ! -f "$SPEC" ]; then
        echo "### $NAME SKIPPED (no spec) $(date -Is)" >> "$QLOG"
        continue
    fi
    echo "### $NAME START $(date -Is)" >> "$QLOG"
    "$ROOT/harness/run.sh" "$NAME" "$SPEC" >/dev/null 2>&1
    echo "### $NAME END status=$? $(date -Is)" >> "$QLOG"
done
