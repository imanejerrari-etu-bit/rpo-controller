#!/usr/bin/env bash
# Sweep: targets x repetitions, randomized order within each repetition block.
# Records pod restart counts and Galera state before/after EVERY run so that
# runs disturbed by a pod restart can be excluded afterwards.
#
# Prerequisites (same terminal): venv active, MYSQL_PASS exported,
# port-forwards 33061-33063 active.
# Usage:  bash run_sweep.sh            (defaults: targets 0.1 0.2 0.3 0.4, 3 reps, 80 TPS)
#         TARGETS="0.2 0.3" REPS=2 bash run_sweep.sh
set -u
TARGETS="${TARGETS:-0.1 0.2 0.3 0.4}"
REPS="${REPS:-3}"
TPS="${TPS:-80}"
DURATION="${DURATION:-600}"
LOG="results/sweep_log.csv"
mkdir -p results
[ -f "$LOG" ] || echo "tag,target,rep,start,end,restarts_before,restarts_after,galera_before,galera_after,flag" > "$LOG"

restarts() { kubectl get pods --no-headers 2>/dev/null | grep -E "pxc-rpo-experiment-(pxc|haproxy)|percona-xtradb-cluster-operator" | awk '{print $4}' | awk '{s+=$1} END{print s+0}'; }
galera()   { kubectl exec pxc-rpo-experiment-pxc-0 -c pxc -- mysql -uroot -p"$MYSQL_PASS" -N -e \
             "SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_size','wsrep_cluster_status','wsrep_local_state_comment')" 2>/dev/null | awk '{print $2}' | paste -sd/ ; }

# sanity checks before starting
[ -n "${MYSQL_PASS:-}" ] || { echo "MYSQL_PASS not exported"; exit 1; }
for p in 33061 33062 33063; do ss -ltn | grep -q ":$p " || { echo "port-forward $p missing"; exit 1; }; done
command -v python >/dev/null || { echo "python not found: activate the venv first (source venv/bin/activate)"; exit 1; }
python -c "import mysql.connector, redis" 2>/dev/null || { echo "python deps missing in this environment (venv not active?)"; exit 1; }
echo "Galera at start: $(galera)  | restarts total: $(restarts)"

for rep in $(seq 1 "$REPS"); do
  for T in $(echo $TARGETS | tr ' ' '\n' | shuf); do
    TAG="t${T}_r${rep}"
    OUT="results/cl_mysql_pi_${TPS}tps_${TAG}.json"
    if [ -f "$OUT" ]; then echo "skip $TAG (exists)"; continue; fi
    RB=$(restarts); GB=$(galera); S=$(date +%H:%M:%S)
    echo "=== $TAG  start $S  restarts=$RB  galera=$GB"
    python -m experiments.closed_loop_mysql --policy pi --target "$T" --tps "$TPS" \
        --duration "$DURATION" --tag "$TAG" > "results/console_${TAG}.log" 2>&1
    RA=$(restarts); GA=$(galera); E=$(date +%H:%M:%S)
    FLAG=ok
    [ "$RB" != "$RA" ] && FLAG=RESTART
    [ "$GA" != "3/Primary/Synced" ] && FLAG="${FLAG}+GALERA"
    [ -f "$OUT" ] || FLAG="${FLAG}+NOFILE"
    echo "$TAG,$T,$rep,$S,$E,$RB,$RA,$GB,$GA,$FLAG" >> "$LOG"
    echo "    end $E  restarts=$RA  galera=$GA  flag=$FLAG"
    if [ ! -f "$OUT" ]; then
      echo "!! run $TAG produced no output file -> stopping the sweep. Last console lines:"
      tail -15 "results/console_${TAG}.log"
      exit 1
    fi
    sleep 60        # let the cluster settle between runs
  done
done
echo "Done. Review $LOG: any FLAG != ok means that run must be excluded or repeated."
