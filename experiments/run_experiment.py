"""
Single Experiment Runner

Usage:
    python run_experiment.py --engine mongodb --tps 250 --run-id 1
    python run_experiment.py --engine redis   --tps 400 --run-id 1
    python run_experiment.py --engine mysql   --tps 80  --run-id 1

Output:
    results/<engine>_run<id>_<tps>tps.json

--- PATCH (Reviewer 1, point 3 — fault injection ground-truth) ---
Added an --ack-log argument (defaults to
results/<engine>_run<id>_<tps>tps.ack.csv) passed through to
make_workload(), so every acknowledged write is logged for later
crash-recovery auditing (see audit_rpo.py). Everything else is
unchanged: this file still runs a normal, uninterrupted 600s
experiment even if the engine crashes mid-run — service.py already
catches proxy/actuator exceptions and continues, so a controlled
crash just shows up as a run of degraded/fallback ticks in the
output JSON, not a process failure.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import redis as redis_lib
import mysql.connector
import pymongo

from rpo_controller.config import (
    RUN_DURATION, WARMUP, COOLDOWN, ENGINES,
    MONGO_URI, MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASS, MYSQL_DB,
    REDIS_HOST, REDIS_PORT, REDIS_PASS,
)
from rpo_controller.service import run_controller
from experiments.workload import make_workload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("run_experiment")


# ─────────────────────────────────────────────────────────────────────────────
# Pre-run setup per engine (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _setup_redis():
    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT,
                        password=REDIS_PASS or None, decode_responses=True)

    # PATCH (fault-injection ground-truth bug, same class as MongoDB/MySQL
    # above): workload.py's TTL=300s does not reliably clear a previous
    # run's "k:*" keys before the next trial starts (trials are often
    # <300s apart), so audit_rpo.py's SCAN-based max-key query can pick
    # up a leftover key from an earlier run instead of this run's own
    # data. Delete them explicitly before every run.
    stale_keys = list(r.scan_iter(match="k:*", count=1000))
    if stale_keys:
        r.delete(*stale_keys)
        log.info("Redis: cleared %d leftover k:* keys from previous runs", len(stale_keys))

    log.info("Redis: triggering BGREWRITEAOF to reset AOF baseline ...")
    r.execute_command("BGREWRITEAOF")

    for _ in range(30):
        info = r.info("persistence")
        if info.get("aof_rewrite_in_progress", 1) == 0:
            log.info("Redis: AOF rewrite complete — baseline reset")
            break
        time.sleep(1)
    else:
        log.warning("Redis: AOF rewrite did not complete in 30 s — proceeding anyway")

    r.config_set("appendfsync", "everysec")
    r.close()
    time.sleep(2)


def _setup_mysql():
    # PATCH (Reviewer 1, point 7): same bug as the control loop's
    # actuator -- a single connection through MYSQL_HOST:MYSQL_PORT
    # only ever reaches one node, and innodb_flush_log_at_trx_commit
    # is per-node, not Galera-replicated. Apply the reset AND the
    # table-drop (fault-injection ground-truth fix, kept from before)
    # on EVERY node.
    from rpo_controller.config import mysql_pxc_pod_hosts
    for host, port in mysql_pxc_pod_hosts():
        conn = mysql.connector.connect(
            host=host, port=port,
            user=MYSQL_USER, password=MYSQL_PASS,
            database=MYSQL_DB, autocommit=True,
        )
        cursor = conn.cursor()
        # Table only needs dropping once really (Galera replicates DDL/
        # data, unlike SET GLOBAL) -- but IF EXISTS makes repeating it
        # per node harmless and keeps this loop uniform/simple.
        cursor.execute("DROP TABLE IF EXISTS writes")
        cursor.execute("SET GLOBAL innodb_flush_log_at_trx_commit = 2")
        cursor.close()
        conn.close()
    log.info("MySQL: writes table dropped, innodb_flush_log_at_trx_commit reset to 2 on all nodes")


def _setup_mongodb():
    client = pymongo.MongoClient(MONGO_URI)
    # PATCH (fault-injection ground-truth bug): without this, the
    # "workload.writes" collection accumulates seq_id-overlapping
    # documents across every run ever executed, so audit_rpo.py's
    # "max recovered seq_id" query returns the cumulative maximum
    # across ALL prior runs instead of this run's own data -- making
    # every crash look like zero data loss regardless of what actually
    # happened. run_ablation.py already does this; run_experiment.py
    # did not, until now.
    client["workload"]["writes"].drop()
    client["admin"].command({"setParameter": 1, "journalCommitInterval": 100})
    client.close()
    log.info("MongoDB: workload.writes dropped, journalCommitInterval reset to 100 ms")


SETUP_FNS = {
    "redis":   _setup_redis,
    "mysql":   _setup_mysql,
    "mongodb": _setup_mongodb,
}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main(engine: str, tps: float, run_id: int, out_dir: Path,
                ack_log: Path, duration: int):
    log.info("=" * 60)
    log.info("RUN START  engine=%s  tps=%.0f  run_id=%d  duration=%ds",
              engine, tps, run_id, duration)
    log.info("ack log -> %s", ack_log)
    log.info("=" * 60)

    # 1. Pre-run setup (BGREWRITEAOF for Redis, parameter reset for others)
    SETUP_FNS[engine]()

    # 2. Start workload generator (warmup period before controller)
    log.info("Starting workload generator (%d s warmup) ...", WARMUP)
    ack_log.parent.mkdir(parents=True, exist_ok=True)
    workload = make_workload(engine, tps, ack_log_path=str(ack_log))   # PATCH
    workload.start()
    await asyncio.sleep(WARMUP)

    # 3. Run PI controller for `duration` seconds (PATCH: overridable, was
    # always RUN_DURATION from config.py)
    log.info("Controller running for %d s ...", duration)
    t_wall_start = time.time()
    results      = await run_controller([engine], duration)
    wall_elapsed = time.time() - t_wall_start

    # 4. Stop workload + cooldown
    log.info("Cooldown %d s ...", COOLDOWN)
    workload.stop()
    await asyncio.sleep(COOLDOWN)

    # 5. Serialize results
    ticks = results[engine]
    record = {
        "engine":       engine,
        "run_id":       run_id,
        "tps":          tps,
        "rpo_star":     ENGINES[engine].rpo_star,
        "kp":           ENGINES[engine].kp,
        "ki":           ENGINES[engine].ki,
        "duration_s":   duration,
        "wall_time_s":  round(wall_elapsed, 1),
        "wall_clock_start": t_wall_start,   # PATCH: needed to map tick "t" -> unix time for audit_rpo.py
        "n_ticks":      len(ticks),
        "ack_log":      str(ack_log),       # PATCH
        "ticks": [
            {
                "t":       round(tk.t, 3),
                "rpo_hat": round(tk.rpo_hat, 4),
                "error":   round(tk.error, 4),
                "dwb":     round(tk.dwb, 4),
                "act":     str(tk.actuator_val),
            }
            for tk in ticks
        ],
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"{engine}_run{run_id:02d}_{int(tps)}tps.json"
    fname.write_text(json.dumps(record, indent=2))
    log.info("Saved -> %s  (%d ticks)", fname, len(ticks))
    return fname


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one RPO controller experiment")
    parser.add_argument("--engine", required=True,
                        choices=["mongodb", "mysql", "redis"])
    parser.add_argument("--tps",    required=True, type=float)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--out-dir", default="results", type=Path)
    parser.add_argument("--duration", default=RUN_DURATION, type=int,
                        help="Override RUN_DURATION (s) — useful to shorten "
                             "fault-injection runs; default matches config.py "
                             "for normal campaign runs.")
    parser.add_argument("--ack-log", default=None, type=Path,
                        help="Path for the write-acknowledgement log "
                             "(default: <out-dir>/<engine>_run<id>_<tps>tps.ack.csv)")
    args = parser.parse_args()

    ack_log = args.ack_log or (
        args.out_dir / f"{args.engine}_run{args.run_id:02d}_{int(args.tps)}tps.ack.csv"
    )

    asyncio.run(main(args.engine, args.tps, args.run_id, args.out_dir, ack_log,
                      args.duration))
