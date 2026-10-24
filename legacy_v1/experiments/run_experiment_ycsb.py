"""
Single Experiment Runner - YCSB VARIANT

Identique a experiments/run_experiment.py, mais remplace le generateur
de charge token-bucket interne (workload.py) par YCSB (benchmark standard),
tout en gardant EXACTEMENT le meme controleur PI (service.py) pour la mesure.

Usage:
    python run_experiment_ycsb.py --engine mongodb --tps 250 --run-id 1
    python run_experiment_ycsb.py --engine mysql   --tps 80  --run-id 1
    python run_experiment_ycsb.py --engine redis   --tps 400 --run-id 1

Output:
    results_ycsb/<engine>_run<id>_<tps>tps.json
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import subprocess
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
from rpo_controller.service import run_controller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("run_experiment_ycsb")

# Chemin vers l'installation YCSB (a adapter si different)
YCSB_DIR = Path(r"C:\Users\HP\Documents\ycsb\YCSB-0.17.0")


# ─────────────────────────────────────────────────────────────────────────────
# Pre-run setup (identique a run_experiment.py original)
# ─────────────────────────────────────────────────────────────────────────────

def _setup_redis():
    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT,
                        password=REDIS_PASS or None, decode_responses=True)
    log.info("Redis: triggering BGREWRITEAOF to reset AOF baseline ...")
    r.execute_command("BGREWRITEAOF")
    for _ in range(30):
        info = r.info("persistence")
        if info.get("aof_rewrite_in_progress", 1) == 0:
            log.info("Redis: AOF rewrite complete - baseline reset")
            break
        time.sleep(1)
    else:
        log.warning("Redis: AOF rewrite did not complete in 30 s - proceeding anyway")
    r.config_set("appendfsync", "everysec")
    # FLUSHALL n'est pas disponible sur ce build Redis (8.8.0) -- nettoyage manuel
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, count=1000)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break
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
    cursor.execute("TRUNCATE TABLE usertable")
    cursor.close()
    conn.close()
    log.info("MySQL: innodb_flush_log_at_trx_commit reset to 2, table truncated")


def _setup_mongodb():
    client = pymongo.MongoClient(MONGO_URI)
    client["admin"].command({"setParameter": 1, "journalCommitInterval": 100})
    client["ycsb"]["usertable"].drop()
    client.close()
    log.info("MongoDB: journalCommitInterval reset to 100 ms, collection dropped")


SETUP_FNS = {
    "redis":   _setup_redis,
    "mysql":   _setup_mysql,
    "mongodb": _setup_mongodb,
}


# ─────────────────────────────────────────────────────────────────────────────
# YCSB workload wrapper (remplace experiments/workload.py)
# ─────────────────────────────────────────────────────────────────────────────

def _ycsb_command(engine: str, tps: float, max_exec_s: int) -> str:
    """Construit la commande YCSB pour l'engine donne."""
    workload_file = "workload_rpo_mongo" if engine == "mongodb" else "workload_rpo"
    base = f'"{YCSB_DIR}\\bin\\ycsb.bat" run {{db}} -s -P workloads/{workload_file} -p threads=32 -target {int(tps)} -p maxexecutiontime={max_exec_s} {{extra}}'

    if engine == "mongodb":
        return base.format(
            db="mongodb",
            extra='-p mongodb.url="mongodb://127.0.0.1:27017/ycsb" -p insertstart=0',
        )
    elif engine == "mysql":
        return base.format(
            db="jdbc",
            extra=(f'-p db.driver=com.mysql.cj.jdbc.Driver '
                   f'-p db.url="jdbc:mysql://127.0.0.1:{MYSQL_PORT}/{MYSQL_DB}" '
                   f'-p db.user={MYSQL_USER} -p db.passwd={MYSQL_PASS} -p insertstart=0'),
        )
    else:  # redis
        return base.format(
            db="redis",
            extra=f'-p redis.host=127.0.0.1 -p redis.port={REDIS_PORT} -p insertstart=0',
        )


class YCSBWorkload:
    """Lance/arrete YCSB comme sous-processus en arriere-plan."""

    def __init__(self, engine: str, tps: float, total_duration_s: int):
        # maxexecutiontime genereux : sera coupe manuellement de toute facon
        self.cmd = _ycsb_command(engine, tps, total_duration_s + 60)
        self.proc: subprocess.Popen | None = None
        self.engine = engine
        self.tps = tps

    def start(self):
        log.info("YCSB: starting workload (%s @ %.0f TPS) ...", self.engine, self.tps)
        self.proc = subprocess.Popen(
            f'cmd /c {self.cmd}',
            cwd=str(YCSB_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

    def stop(self):
        if self.proc is not None:
            log.info("YCSB: stopping workload (PID tree %d) ...", self.proc.pid)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main(engine: str, tps: float, run_id: int, out_dir: Path):
    log.info("=" * 60)
    log.info("RUN START (YCSB)  engine=%s  tps=%.0f  run_id=%d", engine, tps, run_id)
    log.info("=" * 60)

    # 1. Pre-run setup
    SETUP_FNS[engine]()

    # 2. Start YCSB workload (warmup period before controller)
    total_duration = WARMUP + RUN_DURATION
    workload = YCSBWorkload(engine, tps, total_duration)
    workload.start()
    log.info("Starting workload generator (%d s warmup) ...", WARMUP)
    await asyncio.sleep(WARMUP)

    # 3. Run PI controller for full duration (YCSB keeps running underneath)
    log.info("Controller running for %d s ...", RUN_DURATION)
    t_wall_start = time.time()
    results      = await run_controller([engine], RUN_DURATION)
    wall_elapsed = time.time() - t_wall_start

    # 4. Stop YCSB + cooldown
    workload.stop()
    log.info("Cooldown %d s ...", COOLDOWN)
    await asyncio.sleep(COOLDOWN)

    # 5. Serialize results (meme format que l'original, + marqueur ycsb)
    ticks = results[engine]
    record = {
        "engine":         engine,
        "run_id":         run_id,
        "tps":            tps,
        "workload_source": "ycsb-0.17.0",
        "rpo_star":       ENGINES[engine].rpo_star,
        "kp":             ENGINES[engine].kp,
        "ki":             ENGINES[engine].ki,
        "duration_s":     RUN_DURATION,
        "wall_time_s":    round(wall_elapsed, 1),
        "n_ticks":        len(ticks),
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
    parser = argparse.ArgumentParser(description="Run one RPO controller experiment (YCSB-driven)")
    parser.add_argument("--engine", required=True,
                        choices=["mongodb", "mysql", "redis"])
    parser.add_argument("--tps",    required=True, type=float)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--out-dir", default="results_ycsb", type=Path)
    args = parser.parse_args()

    asyncio.run(main(args.engine, args.tps, args.run_id, args.out_dir))
