#!/usr/bin/env bash
# Waits for any in-flight run_scenario.py to exit, then runs the given queue.
while pgrep -f "harness/run_scenario.py" > /dev/null; do sleep 10; done
exec /home/jovyan/FAO/optimized_code_testing/harness/queue.sh "$@"
