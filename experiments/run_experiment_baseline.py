"""
Single Experiment Runner — baseline-comparison variant.

Identical protocol to run_experiment.py (setup, warmup, workload,
controller, cooldown, JSON output) — the ONLY difference is an added
--controller argument selecting which controller class runs the loop
(main / naive_pid / arima_ff), and the output filename / JSON record
tagging which one was used.

Usage:
    python run_experiment_baseline.py --engine mongodb --tps 250 --run-id 1 --controller main
    python run_experiment_baseline.py --engine mongodb --tps 250 --run-id 1 --controller naive_pid
    python run_experiment_baseline.py --engine mongodb --tps 250 --run-id 1 --controller arima_ff

Output:
    results/<engine>_<controller>_run<id>_<tps>tps.json

(main-controller runs land in the SAME schema as your existing
results/<engine>_run<id>_<tps>tps.json files from run_experiment.py,
just with an extra "controller" field and "_main_" in the filename —
your existing analysis/analyze.py should still be able to read these
if it does per-file JSON parsing; the field is additive.)
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import redis as redis_lib
import mysql.connector
import pymongo

from rpo_controller.config import (
    RUN_DURATION, WARMUP, COOLDOWN, ENGINES,
    MONGO_URI, MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASS, MYSQL_DB,
    REDIS_HOST, REDIS_PORT, REDIS_PASS,
)
from rpo_controller.service_baseline import run_controller_variant
from experiments.workload import make_workload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("run_experiment_baseline")


# ─────────────────────────────────────────────────────────────────────────────
# Pre-run setup per engine — IDENTICAL to run_experiment.py
# ─────────────────────────────────────────────────────────────────────────────

def _setup_redis():
    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT,
                        password=REDIS_PASS or None, decode_responses=True)
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

async def main(engine: str, tps: float, run_id: int, controller: str,
                out_dir: Path):
    log.info("=" * 60)
    log.info("RUN START  engine=%s  tps=%.0f  run_id=%d  controller=%s",
              engine, tps, run_id, controller)
    log.info("=" * 60)

    SETUP_FNS[engine]()

    log.info("Starting workload generator (%d s warmup) ...", WARMUP)
    workload = make_workload(engine, tps)
    workload.start()
    await asyncio.sleep(WARMUP)

    log.info("Controller (%s) running for %d s ...", controller, RUN_DURATION)
    t_wall_start = time.time()
    results = await run_controller_variant([engine], RUN_DURATION, controller)
    wall_elapsed = time.time() - t_wall_start

    log.info("Cooldown %d s ...", COOLDOWN)
    workload.stop()
    await asyncio.sleep(COOLDOWN)

    ticks = results[engine]
    record = {
        "engine":       engine,
        "controller":   controller,
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
    fname = out_dir / f"{engine}_{controller}_run{run_id:02d}_{int(tps)}tps.json"
    fname.write_text(json.dumps(record, indent=2))
    log.info("Saved -> %s  (%d ticks)", fname, len(ticks))
    return fname


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run one RPO controller experiment (baseline comparison)")
    parser.add_argument("--engine", required=True,
                        choices=["mongodb", "mysql", "redis"])
    parser.add_argument("--tps",    required=True, type=float)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--controller", required=True,
                        choices=["main", "naive_pid", "arima_ff"])
    parser.add_argument("--out-dir", default="results_baseline", type=Path)
    args = parser.parse_args()

    asyncio.run(main(args.engine, args.tps, args.run_id, args.controller,
                      args.out_dir))
