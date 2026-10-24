"""
Controller overhead per 0.5 s tick: time to read the signal and to apply the actuator.

MongoDB: serverStatus read; setParameter + getParameter (as the controller does).
MySQL PXC: redo-LSN read on node 0; SET GLOBAL + read-back on the three nodes, sequentially.

Usage (repo root, venv active, port-forwards open, MONGO_URI / MYSQL_PASS exported; copy to experiments/):
    python -m experiments.measure_overhead --engine mongo
    python -m experiments.measure_overhead --engine mysql
Output: results/overhead_<engine>.json
"""
import argparse, json, statistics as st, time
from pathlib import Path


def summarise(ms):
    ms = sorted(ms)
    q = lambda f: ms[min(len(ms) - 1, int(f * len(ms)))]
    return {"n": len(ms), "median_ms": round(st.median(ms), 3), "p95_ms": round(q(0.95), 3), "max_ms": round(ms[-1], 3),
            "pct_of_Ts_median": round(100 * st.median(ms) / 500.0, 2)}


def timeit(fn, n):
    out = []
    for _ in range(n):
        t = time.perf_counter(); fn(); out.append((time.perf_counter() - t) * 1000)
    return out


def mongo(n):
    import pymongo
    from rpo_controller.config import MONGO_URI
    c = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    cur = c["admin"].command({"getParameter": 1, "journalCommitInterval": 1})["journalCommitInterval"]

    def read(): c["admin"].command("serverStatus")["wiredTiger"]["log"]
    def act():
        c["admin"].command({"setParameter": 1, "journalCommitInterval": int(cur)})
        c["admin"].command({"getParameter": 1, "journalCommitInterval": 1})
    r = {"signal_read": summarise(timeit(read, n)), "actuation": summarise(timeit(act, n))}
    c.close(); return r


def mysql(n):
    import mysql.connector
    from rpo_controller.config import MYSQL_USER, MYSQL_PASS, MYSQL_DB, mysql_pxc_pod_hosts
    conns = [mysql.connector.connect(host=h, port=p, user=MYSQL_USER, password=MYSQL_PASS, database=MYSQL_DB,
                                     autocommit=True, connection_timeout=10) for h, p in mysql_pxc_pod_hosts()]
    curs = [c.cursor() for c in conns]
    curs[0].execute("SELECT @@GLOBAL.innodb_flush_log_at_trx_commit"); mode = int(curs[0].fetchone()[0])

    def read():
        curs[0].execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Innodb_redo_log_current_lsn','Innodb_redo_log_flushed_to_disk_lsn')")
        curs[0].fetchall()
    def act():
        for cu in curs:
            cu.execute(f"SET GLOBAL innodb_flush_log_at_trx_commit = {mode}")
            cu.execute("SELECT @@GLOBAL.innodb_flush_log_at_trx_commit"); cu.fetchone()
    r = {"signal_read": summarise(timeit(read, n)), "actuation_3_nodes": summarise(timeit(act, n))}
    for c in conns: c.close()
    return r


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["mongo", "mysql"], required=True)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--out-dir", default="results")
    a = ap.parse_args()
    res = mongo(a.n) if a.engine == "mongo" else mysql(a.n)
    print(json.dumps(res, indent=2))
    Path(a.out_dir).mkdir(parents=True, exist_ok=True)
    (Path(a.out_dir) / f"overhead_{a.engine}.json").write_text(json.dumps(res))
