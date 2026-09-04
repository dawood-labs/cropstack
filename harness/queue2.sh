#!/usr/bin/env bash
# queue2.sh <scenario> [...] -- serial RETEST_2 queue over specs_retest2/
ROOT=/home/jovyan/FAO/optimized_code_testing
cd "$ROOT" || exit 1
QUEUE_LOG="$ROOT/logs/retest2_queue.log"
echo "### queue start $(date -Is): $*" >> "$QUEUE_LOG"
for NAME in "$@"; do
    SPEC="$ROOT/specs_retest2/${NAME}.json"
    if [ ! -f "$SPEC" ]; then
        echo "### $NAME SKIPPED (no spec) $(date -Is)" >> "$QUEUE_LOG"; continue
    fi
    echo "### $NAME START $(date -Is)" >> "$QUEUE_LOG"
    "$ROOT/harness/run2.sh" "$NAME" "$SPEC" > /dev/null 2>&1
    echo "### $NAME END status=$? $(date -Is)" >> "$QUEUE_LOG"
done
echo "### queue done $(date -Is)" >> "$QUEUE_LOG"
