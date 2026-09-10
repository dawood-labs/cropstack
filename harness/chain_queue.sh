#!/usr/bin/env bash
# Waits for the currently-running queue.sh to exit, then starts a new queue.
while pgrep -f "harness/queue.sh" > /dev/null; do sleep 15; done
exec /home/jovyan/FAO/optimized_code_testing/harness/queue.sh "$@"
