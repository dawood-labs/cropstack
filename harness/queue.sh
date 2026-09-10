#!/usr/bin/env bash
# queue.sh <scenario> [<scenario> ...] -- runs scenarios strictly back-to-back.
# Each pipeline run already saturates the box, so serial is the only sane order.
ROOT=/home/jovyan/FAO/optimized_code_testing
cd "$ROOT" || exit 1
QUEUE_LOG="$ROOT/logs/queue.log"
echo "### queue start $(date -Is): $*" >> "$QUEUE_LOG"
for NAME in "$@"; do
    SPEC="$ROOT/specs/${NAME}.json"
    if [ ! -f "$SPEC" ]; then
        echo "### $NAME SKIPPED (no spec) $(date -Is)" >> "$QUEUE_LOG"
        continue
    fi
    echo "### $NAME START $(date -Is)" >> "$QUEUE_LOG"
    "$ROOT/harness/run.sh" "$NAME" "$SPEC" > /dev/null 2>&1
    echo "### $NAME END status=$? $(date -Is)" >> "$QUEUE_LOG"
done
echo "### queue done $(date -Is)" >> "$QUEUE_LOG"
