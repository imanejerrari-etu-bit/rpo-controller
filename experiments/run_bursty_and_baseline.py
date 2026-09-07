"""
Bursty Workload Campaign + Threshold Baseline Comparison.

Runs two experiments in sequence:
  A) PI controller under bursty workload (5 runs × 3 engines)
  B) Threshold-based bang-bang baseline vs PI (MongoDB only)

Usage:
    python run_bursty_and_baseline.py
    python run_bursty_and_baseline.py --skip-bursty     # only baseline
    python run_bursty_and_baseline.py --skip-baseline   # only bursty
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

import pymongo
import mysql.connector
import redis as redis_lib

from rpo_controller.config import (
    TS, ENGINES, RUN_DURATION, WARMUP, COOLDOWN, SS_START,
    MONGO_URI, MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASS, MYSQL_DB,
    REDIS_HOST, REDIS_PORT, REDIS_PASS,
)
from rpo_controller.pi_controller import PIController
from rpo_controller.proxies import (
    read_proxy_mongodb, read_proxy_mysql, read_proxy_redis,
    reset_redis_proxy,
)
from rpo_controller.actuators import actuate_mongodb, actuate_mysql, actuate_redis
from experiments.bursty_workload import make_bursty_workload
from experiments.run_experiment import _setup_mongodb, _setup_mysql, _setup_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("bursty")

BURSTY_TPS = {"mongodb": 250, "mysql": 80, "redis": 400}
INTER_RUN_PAUSE = 30

# ─────────────────────────────────────────────────────────────────────────────
# A) Bursty PI experiment
# ─────────────────────────────────────────────────────────────────────────────

async def _run_bursty_engine(engine: str, tps: float, run_id: int) -> list:
    """Run PI controller under bursty workload for one engine."""
    cfg   = ENGINES[engine]
    ctrl  = PIController(cfg)
    ticks = []

    # Setup
    {"mongodb": _setup_mongodb, "mysql": _setup_mysql,
     "redis": _setup_redis}[engine]()

    # Connect
    if engine == "mongodb":
        client = pymongo.MongoClient(MONGO_URI)
        db     = client["admin"]
    elif engine == "mysql":
        conn = mysql.connector.connect(
            host=MYSQL_HOST, port=MYSQL_PORT,
            user=MYSQL_USER, password=MYSQL_PASS,
            database=MYSQL_DB, autocommit=True)
    else:
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT,
                            password=REDIS_PASS or None,
                            decode_responses=True)
        reset_redis_proxy()

    workload = make_bursty_workload(engine, tps, seed=run_id)
    workload.start()
    await asyncio.sleep(WARMUP)

    loop = asyncio.get_running_loop()
    t0   = loop.time()

    while True:
        tick_start = loop.time()
        elapsed    = tick_start - t0
        if elapsed >= RUN_DURATION:
            break

        if engine == "mongodb":
            rpo_hat = await asyncio.to_thread(read_proxy_mongodb, db)
            dwb     = ctrl.tick(rpo_hat)
            val     = await asyncio.to_thread(actuate_mongodb, db, dwb)
        elif engine == "mysql":
            rpo_hat = await asyncio.to_thread(read_proxy_mysql, conn)
            dwb     = ctrl.tick(rpo_hat)
            val     = await asyncio.to_thread(actuate_mysql, conn, dwb)
        else:
            rpo_hat = await asyncio.to_thread(read_proxy_redis, r)
            dwb     = ctrl.tick(rpo_hat)
            val     = await asyncio.to_thread(actuate_redis, r, dwb)

        ticks.append({"t": round(elapsed, 3), "rpo_hat": round(rpo_hat, 4),
                      "dwb": round(dwb, 4), "act": str(val)})

        sleep_s = max(0.0, TS - (loop.time() - tick_start))
        await asyncio.sleep(sleep_s)

    workload.stop()
    if engine == "mongodb":
        client.close()
    elif engine == "mysql":
        conn.close()
    else:
        r.close()
    await asyncio.sleep(COOLDOWN)
    return ticks


async def run_bursty_campaign(n_runs: int, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    for engine in ["mongodb", "mysql", "redis"]:
        tps = BURSTY_TPS[engine]
        for run_id in range(1, n_runs + 1):
            log.info("Bursty %s run %d/%d @ %d TPS", engine, run_id, n_runs, tps)
            ticks = await _run_bursty_engine(engine, tps, run_id)
            fname = out_dir / f"{engine}_bursty_run{run_id:02d}_{tps}tps.json"
            fname.write_text(json.dumps({"engine": engine, "run_id": run_id,
                "tps": tps, "workload": "bursty", "ticks": ticks}, indent=2))
            log.info("Saved → %s", fname)
            await asyncio.sleep(INTER_RUN_PAUSE)


# ─────────────────────────────────────────────────────────────────────────────
# B) Threshold-based baseline (bang-bang controller)
# ─────────────────────────────────────────────────────────────────────────────

class ThresholdController:
    """
    Simple bang-bang baseline: if rpo_hat > RPO* → dwb_min, else → dwb_max.
    No integral, no deadband.
    """
    def __init__(self, rpo_star: float, dwb_min: float = 0.001,
                 dwb_max: float = 0.500):
        self.rpo_star = rpo_star
        self.dwb_min  = dwb_min
        self.dwb_max  = dwb_max

    def tick(self, rpo_hat: float) -> float:
        return self.dwb_min if rpo_hat > self.rpo_star else self.dwb_max


async def run_baseline_comparison(n_runs: int, out_dir: Path):
    """Compare PI vs threshold on MongoDB (uniform workload)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = ENGINES["mongodb"]

    for controller_type in ["pi", "threshold"]:
        for run_id in range(1, n_runs + 1):
            _setup_mongodb()
            client = pymongo.MongoClient(MONGO_URI)
            db     = client["admin"]

            ctrl = (PIController(cfg) if controller_type == "pi"
                    else ThresholdController(cfg.rpo_star))

            from experiments.workload import make_workload
            workload = make_workload("mongodb", 250)
            workload.start()
            await asyncio.sleep(WARMUP)

            loop  = asyncio.get_running_loop()
            t0    = loop.time()
            ticks = []

            while True:
                tick_start = loop.time()
                elapsed    = tick_start - t0
                if elapsed >= RUN_DURATION:
                    break

                rpo_hat = await asyncio.to_thread(read_proxy_mongodb, db)
                dwb     = ctrl.tick(rpo_hat)
                val     = await asyncio.to_thread(actuate_mongodb, db, dwb)
                ticks.append({"t": round(elapsed, 3),
                               "rpo_hat": round(rpo_hat, 4),
                               "dwb": round(dwb, 4)})

                sleep_s = max(0.0, TS - (loop.time() - tick_start))
                await asyncio.sleep(sleep_s)

            workload.stop()
            client.close()
            await asyncio.sleep(COOLDOWN)

            fname = out_dir / f"baseline_{controller_type}_run{run_id:02d}.json"
            fname.write_text(json.dumps({
                "controller": controller_type,
                "engine": "mongodb", "run_id": run_id,
                "ticks": ticks}, indent=2))
            log.info("Saved → %s", fname)
            await asyncio.sleep(INTER_RUN_PAUSE)

    # Print comparison
    import numpy as np, glob
    print("\n" + "=" * 50)
    print("Baseline Comparison: PI vs Threshold (MongoDB)")
    print(f"{'Controller':<12} {'Viol%':>8} {'SS μ':>8} {'SS σ':>8}")
    print("-" * 50)
    for ctype in ["pi", "threshold"]:
        viols, ss_mus = [], []
        for fpath in sorted(out_dir.glob(f"baseline_{ctype}_*.json")):
            rec   = json.loads(fpath.read_text())
            rpo   = np.array([tk["rpo_hat"] for tk in rec["ticks"]])
            t_arr = np.array([tk["t"]       for tk in rec["ticks"]])
            viols.append(float(np.mean(rpo > cfg.rpo_star)) * 100)
            ss_mus.append(float(np.mean(rpo[t_arr > SS_START])))
        print(f"{ctype:<12} {np.mean(viols):>7.1f}% {np.mean(ss_mus):>8.3f} "
              f"{np.std(ss_mus):>8.3f}")
    print("=" * 50)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-runs",         type=int, default=5)
    parser.add_argument("--out-dir",        type=Path,
                        default=Path("results/extra"))
    parser.add_argument("--skip-bursty",    action="store_true")
    parser.add_argument("--skip-baseline",  action="store_true")
    args = parser.parse_args()

    async def main():
        if not args.skip_bursty:
            log.info("=== Bursty workload campaign ===")
            await run_bursty_campaign(args.n_runs, args.out_dir)
        if not args.skip_baseline:
            log.info("=== Threshold baseline comparison ===")
            await run_baseline_comparison(args.n_runs, args.out_dir)

    asyncio.run(main())
