#!/usr/bin/env bash
# MongoDB sweep: TARGETS x REPS, randomized order inside each repetition block.
# Records the mongodb pod restart count and a connection ping around EVERY run,
# stops at the first run that produces no output file.
#
# Prerequisites (same terminal): venv active, MONGO_URI exported (with credentials),
# port-forward 27017 active (kubectl port-forward deploy/mongodb 27017:27017 &).
#
# Usage:  bash run_sweep_mongo.sh 2>&1 | tee results/sweep_mongo_console.log
# Variants (env vars):  TARGETS="0.2 0.3"  REPS=2  TPS=250  DURATION=600  SETTLE=60
#   P-only baseline:    KI=0 PFX=P bash run_sweep_mongo.sh      (files tagged P<target>_r<rep>)
#   Threshold baseline: POLICY=threshold PFX=T TARGETS="0.3" bash run_sweep_mongo.sh        (J in {1,500})
#                       POLICY=threshold PFX=U THR_LOW=150 THR_HIGH=450 TARGETS="0.3" bash run_sweep_mongo.sh
#                       POLICY=threshold PFX=V THR_LOW=250 THR_HIGH=350 TARGETS="0.3" bash run_sweep_mongo.sh
set -u
TARGETS="${TARGETS:-0.1 0.2 0.3 0.4}"
REPS="${REPS:-3}"; TPS="${TPS:-250}"; DURATION="${DURATION:-600}"; SETTLE="${SETTLE:-60}"
KP="${KP:-0.8}"; KI="${KI:-0.20}"; PFX="${PFX:-t}"; POLICY="${POLICY:-pi}"; THR_LOW="${THR_LOW:-1}"; THR_HIGH="${THR_HIGH:-500}"
LOG="results/sweep_log_mongo.csv"
mkdir -p results
[ -f "$LOG" ] || echo "tag,target,rep,start,end,restarts_before,restarts_after,ping_before,ping_after,flag" > "$LOG"

restarts() { kubectl get pods --no-headers 2>/dev/null | grep -E '^mongodb-' | awk '{print $4}' | awk '{s+=$1} END{print s+0}'; }
pingdb()   { python -c "import pymongo,os; pymongo.MongoClient(os.environ['MONGO_URI'],serverSelectionTimeoutMS=4000).admin.command('ping'); print('ok')" 2>/dev/null || echo "FAIL"; }

[ -n "${MONGO_URI:-}" ] || { echo "MONGO_URI not exported"; exit 1; }
command -v python >/dev/null || { echo "python not found: activate the venv (source venv/bin/activate)"; exit 1; }
python -c "import pymongo" 2>/dev/null || { echo "pymongo missing (venv not active?)"; exit 1; }
ss -ltn | grep -q ":27017 " || { echo "port-forward 27017 missing"; exit 1; }
[ "$(pingdb)" = "ok" ] || { echo "MongoDB does not answer (check credentials / port-forward)"; exit 1; }
echo "MongoDB ping ok | pod restarts: $(restarts)"

for rep in $(seq 1 "$REPS"); do
  for T in $(echo $TARGETS | tr ' ' '\n' | shuf); do
    TAG="${PFX}${T}_r${rep}"
    OUT="results/cl_mongo_${POLICY}_${TPS}tps_${TAG}.json"
    if [ -f "$OUT" ]; then echo "skip $TAG (exists)"; continue; fi
    RB=$(restarts); PB=$(pingdb); S=$(date +%H:%M:%S)
    [ "$PB" = "ok" ] || { echo "!! MongoDB not reachable before $TAG -> stop (reopen the port-forward and rerun)"; exit 1; }
    echo "=== $TAG start $S restarts=$RB"
    python -m experiments.closed_loop_mongo --policy "$POLICY" --target "$T" --tps "$TPS" \
        --duration "$DURATION" --kp "$KP" --ki "$KI" --thr-low "$THR_LOW" --thr-high "$THR_HIGH" --tag "$TAG" > "results/console_mongo_${TAG}.log" 2>&1
    RA=$(restarts); PA=$(pingdb); E=$(date +%H:%M:%S); FLAG=ok
    [ "$RB" != "$RA" ] && FLAG=RESTART
    [ "$PA" != "ok" ] && FLAG="${FLAG}+PING"
    [ -f "$OUT" ] || FLAG="${FLAG}+NOFILE"
    echo "$TAG,$T,$rep,$S,$E,$RB,$RA,$PB,$PA,$FLAG" >> "$LOG"
    echo "    end $E restarts=$RA ping=$PA flag=$FLAG"
    if [ ! -f "$OUT" ]; then
      echo "!! run $TAG produced no output file -> stopping. Last console lines:"
      tail -15 "results/console_mongo_${TAG}.log"; exit 1
    fi
    sleep "$SETTLE"
  done
done
echo "Done. Review $LOG: any FLAG != ok means that run must be excluded or repeated."
