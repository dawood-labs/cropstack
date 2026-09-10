#!/usr/bin/env bash
# Scenario C4: SIGKILL a run during NDVI tile inference, then resume it.
ROOT=/home/jovyan/FAO/optimized_code_testing
cd "$ROOT" || exit 1
LOG="$ROOT/logs/C4a_kill_target.log"
KILLLOG="$ROOT/logs/C4_kill_report.txt"
: > "$KILLLOG"

echo "[$(date -Is)] launching C4a_kill_target" | tee -a "$KILLLOG"
nohup "$ROOT/harness/run.sh" C4a_kill_target "$ROOT/specs/C4a_kill_target.json" >/dev/null 2>&1 &

# Wait for inference to actually begin.
for _ in $(seq 1 240); do
    grep -q "Running RF inference" "$LOG" 2>/dev/null && break
    sleep 5
done
if ! grep -q "Running RF inference" "$LOG" 2>/dev/null; then
    echo "[$(date -Is)] inference never started; aborting kill test" | tee -a "$KILLLOG"
    exit 1
fi
echo "[$(date -Is)] inference started: $(grep -h 'Running RF inference' "$LOG" | tail -1)" | tee -a "$KILLLOG"

# Let the first round of tiles finish, then kill in the second round.
sleep 25

PID=$(pgrep -f "run_scenario.py --name C4a_kill_target" | head -1)
echo "[$(date -Is)] state immediately before SIGKILL:" | tee -a "$KILLLOG"
ls -la "$ROOT/runs/C_kill_cane_2025/1_ndvi_run_1/tile_predictions/" 2>/dev/null | tee -a "$KILLLOG"
pgrep -f "run_scenario.py --name C4a_kill_target" | tee -a "$KILLLOG"

if [ -n "$PID" ]; then
    # Kill the parent and every worker it spawned -- a real crash, no cleanup handlers.
    CHILDREN=$(pgrep -P "$PID")
    echo "[$(date -Is)] SIGKILL parent $PID children: $CHILDREN" | tee -a "$KILLLOG"
    kill -9 $CHILDREN 2>/dev/null
    kill -9 "$PID" 2>/dev/null
else
    echo "[$(date -Is)] target already gone (inference finished too fast)" | tee -a "$KILLLOG"
fi
sleep 5
pkill -9 -f "run_scenario.py --name C4a_kill_target" 2>/dev/null
pkill -9 -f "harness/run.sh C4a_kill_target" 2>/dev/null
sleep 2

echo "[$(date -Is)] state AFTER kill:" | tee -a "$KILLLOG"
ls -la "$ROOT/runs/C_kill_cane_2025/1_ndvi_run_1/" 2>/dev/null | tee -a "$KILLLOG"
ls -la "$ROOT/runs/C_kill_cane_2025/1_ndvi_run_1/tile_predictions/" 2>/dev/null | tee -a "$KILLLOG"
ls -la "$ROOT/runs/C_kill_cane_2025/1_ndvi_run_1/raw_ndvi_tiles/" 2>/dev/null | tee -a "$KILLLOG"

echo "[$(date -Is)] launching C4b_resume_after_kill" | tee -a "$KILLLOG"
"$ROOT/harness/run.sh" C4b_resume_after_kill "$ROOT/specs/C4b_resume_after_kill.json" >/dev/null 2>&1
echo "[$(date -Is)] resume finished status=$?" | tee -a "$KILLLOG"
