"""
Single Experiment Runner

Usage:
    python run_experiment.py --engine mongodb --tps 250 --run-id 1
    python run_experiment.py --engine redis   --tps 400 --run-id 1
    python run_experiment.py --engine mysql   --tps 80  --run-id 1

Output:
    results/<engine>_run<id>_<tps>tps.json
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
# Pre-run setup per engine
# ─────────────────────────────────────────────────────────────────────────────

def _setup_redis():
    """
    FIX for the cold-start artifact (Run 1 = 100% violations in the paper).

    BGREWRITEAOF must be called before each run to reset the AOF file
    and the growth ratio g back to 1.0. Without this, the AOF file
    grows from a previous run's baseline, causing the proxy to saturate
    immediately at run start.
    """
    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT,
                        password=REDIS_PASS or None, decode_responses=True)
    log.info("Redis: triggering BGREWRITEAOF to reset AOF baseline ...")
    r.execute_command("BGREWRITEAOF")

    # Wait for rewrite to complete (poll aof_rewrite_in_progress)
    for _ in range(30):
        info = r.info("persistence")
        if info.get("aof_rewrite_in_progress", 1) == 0:
            log.info("Redis: AOF rewrite complete — baseline reset")
            break
        time.sleep(1)
    else:
        log.warning("Redis: AOF rewrite did not complete in 30 s — proceeding anyway")

    # Also reset appendfsync to everysec (neutral start)
    r.config_set("appendfsync", "everysec")
    r.close()
    time.sleep(2)    # let Redis settle


def _setup_mysql():
    """Reset MySQL innodb_flush_log to neutral (v=2) before run."""
    conn = mysql.connector.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASS,
        database=MYSQL_DB, autocommit=True,
    )
    cursor = conn.cursor()
    cursor.execute("SET GLOBAL innodb_flush_log_at_trx_commit = 2")
    cursor.close()
    conn.close()
    log.info("MySQL: innodb_flush_log_at_trx_commit reset to 2")


def _setup_mongodb():
    """Reset MongoDB journalCommitInterval to 100 ms (neutral) before run."""
    client = pymongo.MongoClient(MONGO_URI)
    client["admin"].command({"setParameter": 1, "journalCommitInterval": 100})
    client.close()
    log.info("MongoDB: journalCommitInterval reset to 100 ms")


SETUP_FNS = {
    "redis":   _setup_redis,
    "mysql":   _setup_mysql,
    "mongodb": _setup_mongodb,
}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main(engine: str, tps: float, run_id: int, out_dir: Path):
    log.info("=" * 60)
    log.info("RUN START  engine=%s  tps=%.0f  run_id=%d", engine, tps, run_id)
    log.info("=" * 60)

    # 1. Pre-run setup (BGREWRITEAOF for Redis, parameter reset for others)
    SETUP_FNS[engine]()

    # 2. Start workload generator (warmup period before controller)
    log.info("Starting workload generator (%d s warmup) ...", WARMUP)
    workload = make_workload(engine, tps)
    workload.start()
    await asyncio.sleep(WARMUP)

    # 3. Run PI controller for full duration
    log.info("Controller running for %d s ...", RUN_DURATION)
    t_wall_start = time.time()
    results      = await run_controller([engine], RUN_DURATION)
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
        "duration_s":   RUN_DURATION,
        "wall_time_s":  round(wall_elapsed, 1),
        "n_ticks":      len(ticks),
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
    log.info("Saved → %s  (%d ticks)", fname, len(ticks))
    return fname


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one RPO controller experiment")
    parser.add_argument("--engine", required=True,
                        choices=["mongodb", "mysql", "redis"])
    parser.add_argument("--tps",    required=True, type=float)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--out-dir", default="results", type=Path)
    args = parser.parse_args()

    asyncio.run(main(args.engine, args.tps, args.run_id, args.out_dir))
