"""
Step-response experiment (open loop, NO controller).

Goal: measure the real static map  actuator mode  ->  RPO proxy  for the
discrete engines, so that Proposition 1 can be stated on the true plant
image instead of on the actuator set.

Design: ABAB (A,B,A,B) so that slow drift (Redis AOF growth ratio g,
InnoDB log growth) can be separated from the effect of the mode itself.
After every switch the mode is READ BACK on every node to prove that the
actuation really took effect.

Usage (from the repo root, port-forwards running, same as run_campaign):
    python -m experiments.step_response --engine mysql --tps 80  --hold 300
    python -m experiments.step_response --engine redis --tps 400 --hold 300
Output: results/step_<engine>_<tps>tps.json
"""
from __future__ import annotations
import argparse, json, logging, statistics as st, time
from pathlib import Path

import redis as redis_lib
import mysql.connector

from rpo_controller.config import (
    TS, REDIS_HOST, REDIS_PORT, REDIS_PASS,
    MYSQL_USER, MYSQL_PASS, MYSQL_DB, mysql_pxc_pod_hosts,
)
from rpo_controller.proxies import (
    read_proxy_mysql, read_proxy_redis, reset_redis_proxy,
)
from experiments.run_experiment import SETUP_FNS
from experiments.workload import make_workload

log = logging.getLogger("step_response")

# Levels forced in open loop (ABAB). Add "no" for Redis with --include-no.
LEVELS = {
    "mysql": [1, 2, 1, 2],                                   # innodb_flush_log_at_trx_commit
    "redis": ["always", "everysec", "always", "everysec"],   # appendfsync
}


# ── actuation + read-back ────────────────────────────────────────────────────
def _mysql_open():
    return [mysql.connector.connect(host=h, port=p, user=MYSQL_USER,
                                    password=MYSQL_PASS, database=MYSQL_DB,
                                    autocommit=True, connection_timeout=10)
            for h, p in mysql_pxc_pod_hosts()]


def set_mode(engine, handle, mode):
    """Apply mode; return the value READ BACK from every node (list)."""
    if engine == "mysql":
        out = []
        for c in handle:
            cur = c.cursor()
            cur.execute(f"SET GLOBAL innodb_flush_log_at_trx_commit = {mode}")
            cur.execute("SELECT @@GLOBAL.innodb_flush_log_at_trx_commit")
            out.append(int(cur.fetchone()[0]))
            cur.close()
        return out
    handle.config_set("appendfsync", mode)
    return [handle.config_get("appendfsync")["appendfsync"]]


# ── one sample: proxy + raw components ───────────────────────────────────────
def sample(engine, handle):
    if engine == "mysql":
        rpo = read_proxy_mysql(handle[0])
        raw = {}
        cur = handle[0].cursor()
        cur.execute("SHOW GLOBAL STATUS WHERE Variable_name IN "
                    "('wsrep_local_send_queue','Threads_running',"
                    "'Innodb_log_waits','Innodb_os_log_fsyncs')")
        raw = {k: float(v) for k, v in cur.fetchall()}
        cur.close()
        return rpo, raw
    rpo = read_proxy_redis(handle)
    # log EVERY numeric field of INFO persistence + ops/s: which ones react to the mode?
    raw = {k: float(v) for k, v in handle.info("persistence").items()
           if isinstance(v, (int, float)) and not isinstance(v, bool)}
    raw["instantaneous_ops_per_sec"] = float(handle.info("stats").get("instantaneous_ops_per_sec", 0))
    return rpo, raw


# ── main ─────────────────────────────────────────────────────────────────────
def main(engine, tps, hold, warmup, include_no, out_dir):
    SETUP_FNS[engine]()                       # clean state (same as campaigns)
    handle = _mysql_open() if engine == "mysql" else redis_lib.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASS or None,
        decode_responses=True, socket_timeout=5)
    if engine == "redis":
        reset_redis_proxy()

    levels = list(LEVELS[engine])
    if engine == "redis" and include_no:
        levels += ["no", "always"]

    wl = make_workload(engine, tps)
    wl.start()
    segments = []
    try:
        t0 = time.time()
        for idx, mode in enumerate(levels):
            readback = set_mode(engine, handle, mode)
            log.info("segment %d: mode=%s readback=%s", idx, mode, readback)
            seg = {"idx": idx, "mode": mode, "readback": readback, "ticks": []}
            t_seg = time.time()
            nxt = t_seg
            while time.time() - t_seg < hold + (warmup if idx == 0 else 0):
                rpo, raw = sample(engine, handle)
                seg["ticks"].append({"t": round(time.time() - t0, 3),
                                     "seg_t": round(time.time() - t_seg, 3),
                                     "rpo_hat": rpo, **raw})
                nxt += TS
                time.sleep(max(0.0, nxt - time.time()))
            segments.append(seg)
    finally:
        wl.stop()

    # ── summary: steady part of each segment = last 60 % ─────────────────────
    summary = []
    for seg in segments:
        tk = seg["ticks"]
        tail = [x["rpo_hat"] for x in tk[int(0.4 * len(tk)):]]
        summary.append({"idx": seg["idx"], "mode": seg["mode"],
                        "mean": round(st.mean(tail), 4),
                        "std": round(st.pstdev(tail), 4),
                        "readback_ok": all(str(r) == str(seg["mode"])
                                           for r in seg["readback"])})
    print("\nSegment  mode       proxy mean   std     readback_ok")
    for s in summary:
        print(f"{s['idx']:>7}  {str(s['mode']):<9}  {s['mean']:>10}  "
              f"{s['std']:>6}  {s['readback_ok']}")

    # per-segment means of every raw field -> show the ones that differ between modes
    skip = {"t", "seg_t", "rpo_hat"}
    keys = sorted({k for seg in segments for x in seg["ticks"] for k in x if k not in skip})
    means = {k: [st.mean(x[k] for x in seg["ticks"][int(0.4 * len(seg["ticks"])):] if k in x)
                 if any(k in x for x in seg["ticks"]) else float("nan") for seg in segments] for k in keys}
    print("\nRaw fields whose segment means differ by >20 % between segments (candidate proxies):")
    for k, v in means.items():
        lo, hi = min(v), max(v)
        if hi > 0 and (hi - lo) / hi > 0.2:
            print(f"  {k:<32} " + "  ".join(f"{m:12.3f}" for m in v))
    out = Path(out_dir) / f"step_{engine}_{int(tps)}tps.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"engine": engine, "tps": tps, "hold_s": hold,
                               "summary": summary, "segments": segments}))
    print("saved", out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["mysql", "redis"], required=True)
    ap.add_argument("--tps", type=float, required=True)
    ap.add_argument("--hold", type=int, default=300, help="seconds per segment")
    ap.add_argument("--warmup", type=int, default=30)
    ap.add_argument("--include-no", action="store_true",
                    help="Redis only: also test appendfsync=no (adds 2 segments)")
    ap.add_argument("--out-dir", default="results")
    a = ap.parse_args()
    main(a.engine, a.tps, a.hold, a.warmup, a.include_no, a.out_dir)
