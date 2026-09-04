#!/usr/bin/env bash
# run2.sh <scenario_name> <spec.json>  -- RETEST_2 runner.
# Identical to run.sh except PYTHONPATH: the shared standard-libraries worktree moved
# from 30c67408 (campaign 1 + retest 1) to 824850c, and the old tree no longer exists
# on this box.
set -o pipefail
ROOT=/home/jovyan/FAO/optimized_code_testing
export PYTHONPATH=/home/jovyan/shared/git/standard-libraries/.worktrees/824850c677f49ef5b23af6040e9d2b165e586996
export GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
NAME="$1"; SPEC="$2"
mkdir -p "$ROOT/logs" "$ROOT/metrics"
echo "=== $NAME starting $(date -Is) ==="
"$ROOT/cropstack_venv/bin/python" "$ROOT/harness/run_scenario.py" \
    --name "$NAME" --spec "$SPEC" 2>&1 | tee "$ROOT/logs/${NAME}.log"
STATUS=${PIPESTATUS[0]}
echo "=== $NAME finished status=$STATUS $(date -Is) ==="
exit $STATUS
