#!/usr/bin/env bash
# Load-invariance sweep (closed loop, fixed target, several offered loads), MySQL or MongoDB.
# Randomized order over (load, repetition); stops at the first run with no output file.
#
# Usage:
#   ENGINE=mysql TPS_LIST="60 100" bash run_load.sh 2>&1 | tee results/load_mysql_console.log
#   ENGINE=mongo TPS_LIST="100 400" bash run_load.sh 2>&1 | tee results/load_mongo_console.log
# Env: TARGET=0.3  REPS=3  DURATION=600  SETTLE=60
# Prerequisites (same terminal): venv active; mysql -> MYSQL_PASS + port-forwards 33061-33063;
#                                mongo -> MONGO_URI + port-forward 27017. Do NOT run both engines at once.
set -u
ENGINE="${ENGINE:-}"; TARGET="${TARGET:-0.3}"; REPS="${REPS:-3}"; DURATION="${DURATION:-600}"; SETTLE="${SETTLE:-60}"
case "$ENGINE" in mysql|mongo) ;; *) echo "set ENGINE=mysql or ENGINE=mongo"; exit 1;; esac
[ -n "${TPS_LIST:-}" ] || { echo "set TPS_LIST, e.g. TPS_LIST=\"60 100\""; exit 1; }
LOG="results/sweep_log_load.csv"; mkdir -p results
[ -f "$LOG" ] || echo "tag,engine,tps,rep,start,end,restarts_before,restarts_after,state_before,state_after,flag" > "$LOG"

if [ "$ENGINE" = mysql ]; then
  restarts() { kubectl get pods --no-headers 2>/dev/null | grep -E "pxc-rpo-experiment-(pxc|haproxy)|percona-xtradb-cluster-operator" | awk '{print $4}' | awk '{s+=$1} END{print s+0}'; }
  state()    { kubectl exec pxc-rpo-experiment-pxc-0 -c pxc -- mysql -uroot -p"$MYSQL_PASS" -N -e \
               "SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_size','wsrep_cluster_status','wsrep_local_state_comment')" 2>/dev/null | awk '{print $2}' | paste -sd/ ; }
  GOOD="3/Primary/Synced"
  [ -n "${MYSQL_PASS:-}" ] || { echo "MYSQL_PASS not exported"; exit 1; }
  for p in 33061 33062 33063; do ss -ltn | grep -q ":$p " || { echo "port-forward $p missing"; exit 1; }; done
else
  restarts() { kubectl get pods --no-headers 2>/dev/null | grep -E '^mongodb-' | awk '{print $4}' | awk '{s+=$1} END{print s+0}'; }
  state()    { python -c "import pymongo,os; pymongo.MongoClient(os.environ['MONGO_URI'],serverSelectionTimeoutMS=4000).admin.command('ping'); print('ok')" 2>/dev/null || echo "FAIL"; }
  GOOD="ok"
  [ -n "${MONGO_URI:-}" ] || { echo "MONGO_URI not exported"; exit 1; }
  ss -ltn | grep -q ":27017 " || { echo "port-forward 27017 missing"; exit 1; }
fi
command -v python >/dev/null || { echo "python not found: activate the venv"; exit 1; }
python -c "import pymongo, mysql.connector" 2>/dev/null || { echo "python deps missing (venv not active?)"; exit 1; }
[ "$(state)" = "$GOOD" ] || { echo "$ENGINE not healthy at start: $(state)"; exit 1; }
echo "$ENGINE healthy ($(state)) | restarts total: $(restarts)"

for rep in $(seq 1 "$REPS"); do
  for TPS in $(echo $TPS_LIST | tr ' ' '\n' | shuf); do
    TAG="L${TPS}_r${rep}"; LTAG="${ENGINE}_${TAG}"
    OUT="results/cl_${ENGINE}_pi_${TPS}tps_${TAG}.json"
    if [ -f "$OUT" ]; then echo "skip $LTAG (exists)"; continue; fi
    RB=$(restarts); SB=$(state); S=$(date +%H:%M:%S)
    [ "$SB" = "$GOOD" ] || { echo "!! $ENGINE not healthy before $LTAG ($SB) -> stop"; exit 1; }
    echo "=== $LTAG start $S restarts=$RB"
    python -m experiments.closed_loop_${ENGINE} --policy pi --target "$TARGET" --tps "$TPS" \
        --duration "$DURATION" --tag "$TAG" > "results/console_${LTAG}.log" 2>&1
    RA=$(restarts); SA=$(state); E=$(date +%H:%M:%S); FLAG=ok
    [ "$RB" != "$RA" ] && FLAG=RESTART
    [ "$SA" != "$GOOD" ] && FLAG="${FLAG}+STATE"
    [ -f "$OUT" ] || FLAG="${FLAG}+NOFILE"
    echo "$LTAG,$ENGINE,$TPS,$rep,$S,$E,$RB,$RA,$SB,$SA,$FLAG" >> "$LOG"
    echo "    end $E restarts=$RA state=$SA flag=$FLAG"
    if [ ! -f "$OUT" ]; then
      echo "!! $LTAG produced no output file -> stopping. Last console lines:"; tail -15 "results/console_${LTAG}.log"; exit 1
    fi
    sleep "$SETTLE"
  done
done
echo "Done. Review $LOG: any FLAG != ok means that run must be excluded or repeated."
