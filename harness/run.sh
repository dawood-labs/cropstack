#!/usr/bin/env bash
# run.sh <scenario_name> <spec.json>  -- runs one instrumented scenario, logging to logs/
set -o pipefail
ROOT=/home/jovyan/FAO/optimized_code_testing
export PYTHONPATH=/home/jovyan/shared/git/standard-libraries/.worktrees/30c67408d63a3eff3167aed568143ed9626e1f18
export GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
NAME="$1"; SPEC="$2"
mkdir -p "$ROOT/logs" "$ROOT/metrics"
echo "=== $NAME starting $(date -Is) ===" 
"$ROOT/cropstack_venv/bin/python" "$ROOT/harness/run_scenario.py" \
    --name "$NAME" --spec "$SPEC" 2>&1 | tee "$ROOT/logs/${NAME}.log"
STATUS=${PIPESTATUS[0]}
echo "=== $NAME finished status=$STATUS $(date -Is) ==="
exit $STATUS
