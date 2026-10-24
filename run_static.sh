#!/usr/bin/env bash
# Same-session static baselines: static1 and static2, REPS repetitions, interleaved & randomized.
# Same checks as run_sweep.sh (restarts + Galera recorded around each run, stop on failure).
# Prerequisites: venv active, MYSQL_PASS exported, port-forwards 33061-33063 active.
# Usage: bash run_static.sh 2>&1 | tee results/static_console.log
set -u
REPS="${REPS:-3}"; TPS="${TPS:-80}"; DURATION="${DURATION:-600}"; TARGET="${TARGET:-0.3}"
LOG="results/sweep_log.csv"
mkdir -p results
[ -f "$LOG" ] || echo "tag,target,rep,start,end,restarts_before,restarts_after,galera_before,galera_after,flag" > "$LOG"
restarts() { kubectl get pods --no-headers 2>/dev/null | grep -E "pxc-rpo-experiment-(pxc|haproxy)|percona-xtradb-cluster-operator" | awk '{print $4}' | awk '{s+=$1} END{print s+0}'; }
galera()   { kubectl exec pxc-rpo-experiment-pxc-0 -c pxc -- mysql -uroot -p"$MYSQL_PASS" -N -e \
             "SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_size','wsrep_cluster_status','wsrep_local_state_comment')" 2>/dev/null | awk '{print $2}' | paste -sd/ ; }
[ -n "${MYSQL_PASS:-}" ] || { echo "MYSQL_PASS not exported"; exit 1; }
command -v python >/dev/null || { echo "python not found: activate the venv first"; exit 1; }
python -c "import mysql.connector, redis" 2>/dev/null || { echo "python deps missing (venv not active?)"; exit 1; }
for p in 33061 33062 33063; do ss -ltn | grep -q ":$p " || { echo "port-forward $p missing"; exit 1; }; done
echo "Galera at start: $(galera) | restarts total: $(restarts)"
for rep in $(seq 1 "$REPS"); do
  for P in $(printf "static1\nstatic2\n" | shuf); do
    TAG="r${rep}"; OUT="results/cl_mysql_${P}_${TPS}tps_${TAG}.json"; LTAG="${P}_${TAG}"
    [ -f "$OUT" ] && { echo "skip $LTAG (exists)"; continue; }
    RB=$(restarts); GB=$(galera); S=$(date +%H:%M:%S)
    echo "=== $LTAG start $S restarts=$RB galera=$GB"
    python -m experiments.closed_loop_mysql --policy "$P" --target "$TARGET" --tps "$TPS" \
        --duration "$DURATION" --tag "$TAG" > "results/console_${LTAG}.log" 2>&1
    RA=$(restarts); GA=$(galera); E=$(date +%H:%M:%S); FLAG=ok
    [ "$RB" != "$RA" ] && FLAG=RESTART
    [ "$GA" != "3/Primary/Synced" ] && FLAG="${FLAG}+GALERA"
    [ -f "$OUT" ] || FLAG="${FLAG}+NOFILE"
    echo "$LTAG,$TARGET,$rep,$S,$E,$RB,$RA,$GB,$GA,$FLAG" >> "$LOG"
    echo "    end $E restarts=$RA galera=$GA flag=$FLAG"
    [ -f "$OUT" ] || { echo "!! no output -> stop"; tail -15 "results/console_${LTAG}.log"; exit 1; }
    sleep 60
  done
done
echo "Done."
