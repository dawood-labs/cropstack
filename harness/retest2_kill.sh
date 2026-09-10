#!/usr/bin/env bash
# retest2_kill.sh <scenario> <spec> <marker-regex> <post-marker-sleep> <report>
# Launches a run in its own process group, waits for <marker-regex> to appear in the
# run log, sleeps <post-marker-sleep>, then SIGKILLs the whole group -- a real crash,
# no cleanup handlers, exactly like this box being culled.
ROOT=/home/jovyan/FAO/optimized_code_testing
NAME="$1"; SPEC="$2"; MARKER="$3"; DELAY="$4"; REPORT="$5"
LOG="$ROOT/logs/${NAME}.log"
: > "$REPORT"; : > "$LOG"

say() { echo "[$(date -Is)] $*" | tee -a "$REPORT"; }

say "launching $NAME (own process group)"
setsid "$ROOT/harness/run2.sh" "$NAME" "$SPEC" >/dev/null 2>&1 &
LAUNCH=$!
sleep 1
PGID=$(ps -o pgid= -p "$LAUNCH" 2>/dev/null | tr -d ' ')
say "process group $PGID"

for _ in $(seq 1 1200); do
    grep -qE "$MARKER" "$LOG" 2>/dev/null && break
    sleep 0.2
done
if ! grep -qE "$MARKER" "$LOG" 2>/dev/null; then
    say "MARKER '$MARKER' never appeared -- aborting kill test"
    kill -9 -"$PGID" 2>/dev/null
    exit 1
fi
say "marker hit: $(grep -aoE "$MARKER" "$LOG" | tail -1)"
sleep "$DELAY"

say "process tree immediately before SIGKILL:"
ps -o pid,ppid,pgid,etime,rss,cmd -g "$PGID" 2>/dev/null | tee -a "$REPORT"
kill -9 -"$PGID" 2>/dev/null
sleep 2
pkill -9 -f "run_scenario.py --name $NAME" 2>/dev/null
sleep 1
say "survivors after SIGKILL: $(pgrep -f "run_scenario.py --name $NAME" | tr '\n' ' ')"
say "kill complete"
