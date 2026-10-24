"""
Candidate MySQL durability proxy: unflushed redo gap.

  gap_bytes  = Innodb_redo_log_current_lsn - Innodb_redo_log_flushed_to_disk_lsn
  gap_seconds = gap_bytes / redo write rate   (bytes/s, from LSN growth)

Open loop (NO controller), ABAB over innodb_flush_log_at_trx_commit {1,2},
under a LIVE workload started by this script. The gap is read on node 0
(the node receiving the writes) and on node 1 (Galera applier) for comparison.

Usage (repo root, port-forwards 33061-33063 active, MYSQL_PASS exported):
    python -m experiments.redo_gap_probe --tps 80 --hold 60
Output: results/redo_gap_<tps>tps.json
"""
from __future__ import annotations
import argparse, json, logging, os, statistics as st, time
from pathlib import Path

import mysql.connector

from rpo_controller.config import MYSQL_USER, MYSQL_PASS, MYSQL_DB, mysql_pxc_pod_hosts
from experiments.run_experiment import SETUP_FNS
from experiments.workload import make_workload

log = logging.getLogger("redo_gap_probe")
LSN = ("Innodb_redo_log_current_lsn", "Innodb_redo_log_flushed_to_disk_lsn")


def read_lsn(cur):
    cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN (%s,%s)", LSN)
    d = {k: float(v) for k, v in cur.fetchall()}
    return d[LSN[0]], d[LSN[1]]


def main(tps, hold, settle, out_dir):
    SETUP_FNS["mysql"]()
    conns = [mysql.connector.connect(host=h, port=p, user=MYSQL_USER,
                                     password=MYSQL_PASS, database=MYSQL_DB,
                                     autocommit=True, connection_timeout=10)
             for h, p in mysql_pxc_pod_hosts()]
    curs = [c.cursor() for c in conns]

    wl = make_workload("mysql", tps)
    wl.start()
    time.sleep(10)                                   # let the workload ramp up
    results = []
    try:
        for idx, mode in enumerate([1, 2, 1, 2]):
            for cur in curs:
                cur.execute(f"SET GLOBAL innodb_flush_log_at_trx_commit = {mode}")
            time.sleep(settle)                       # discard transient after switch
            samples = {0: [], 1: []}
            t_start = time.time()
            lsn0_start = read_lsn(curs[0])[0]
            nxt = t_start
            while time.time() - t_start < hold:
                for node in (0, 1):
                    cur_lsn, fl_lsn = read_lsn(curs[node])
                    samples[node].append(cur_lsn - fl_lsn)
                nxt += 0.5
                time.sleep(max(0.0, nxt - time.time()))
            dt = time.time() - t_start
            rate = (read_lsn(curs[0])[0] - lsn0_start) / dt
            row = {"idx": idx, "mode": mode, "rate_Bps": round(rate, 1)}
            for node in (0, 1):
                g = samples[node]
                row[f"node{node}"] = {
                    "gap_mean_B": round(st.mean(g), 1),
                    "gap_max_B": max(g),
                    "frac_ticks_gap_gt0": round(sum(x > 0 for x in g) / len(g), 3),
                    "gap_mean_s": round(st.mean(g) / max(rate, 1.0), 4),
                    "gap_max_s": round(max(g) / max(rate, 1.0), 4),
                }
            results.append(row)
            log.info("%s", row)
    finally:
        wl.stop()
        for cur in curs:
            cur.execute("SET GLOBAL innodb_flush_log_at_trx_commit = 2")
        for c in conns:
            c.close()

    print("\nseg mode  rate(B/s)   node0: gap_mean(s) gap_max(s) frac>0   node1: gap_mean(s)")
    for r in results:
        n0, n1 = r["node0"], r["node1"]
        print(f"{r['idx']:>3} {r['mode']:>4} {r['rate_Bps']:>10}   "
              f"{n0['gap_mean_s']:>14} {n0['gap_max_s']:>10} {n0['frac_ticks_gap_gt0']:>7}   "
              f"{n1['gap_mean_s']:>14}")
    out = Path(out_dir) / f"redo_gap_{int(tps)}tps.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tps": tps, "hold_s": hold, "results": results}))
    print("saved", out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--tps", type=float, default=80)
    ap.add_argument("--hold", type=int, default=60, help="seconds per segment")
    ap.add_argument("--settle", type=int, default=5)
    ap.add_argument("--out-dir", default="results")
    a = ap.parse_args()
    main(a.tps, a.hold, a.settle, a.out_dir)
