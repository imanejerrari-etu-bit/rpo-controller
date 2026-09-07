"""
Redis Temporal Dithering Experiment Runner.

Runs 20 independent 600s experiments with the dithering actuator
enabled, then compares results against the original discrete actuator.

Usage:
    python run_dithering.py                    # 20 runs
    python run_dithering.py --n-runs 5         # quick test
    python run_dithering.py --start-run 6      # resume
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import redis as redis_lib

from rpo_controller.config import (
    TS, ENGINES, RUN_DURATION, WARMUP, COOLDOWN, SS_START,
    REDIS_HOST, REDIS_PORT, REDIS_PASS,
)
from rpo_controller.pi_controller import PIController
from rpo_controller.proxies import read_proxy_redis, reset_redis_proxy
from rpo_controller.dithering import TemporalDitheringActuator
from experiments.workload import make_workload
from experiments.run_experiment import _setup_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("dithering_exp")

# TPS range matching original Redis campaign
TPS_ORIGINAL = [400, 300, 500, 350, 450]
TPS_RANGE    = (280, 520)
INTER_RUN_PAUSE = 30


async def run_dithering_experiment(
    tps: float,
    run_id: int,
    out_dir: Path,
) -> Path:
    """Single Redis run with temporal dithering actuator."""
    cfg     = ENGINES["redis"]
    ctrl    = PIController(cfg)
    dither  = TemporalDitheringActuator(window=10)
    ticks   = []

    log.info("=" * 60)
    log.info("DITHER RUN %02d  tps=%.0f  RPO*=%.1fs", run_id, tps, cfg.rpo_star)
    log.info("=" * 60)

    # Pre-run setup
    _setup_redis()

    # Connect
    r = redis_lib.Redis(
        host=REDIS_HOST, port=REDIS_PORT,
        password=REDIS_PASS or None,
        decode_responses=True, socket_timeout=5,
    )

    # Workload
    workload = make_workload("redis", tps)
    workload.start()
    await asyncio.sleep(WARMUP)

    reset_redis_proxy()
    loop  = asyncio.get_running_loop()
    t0    = loop.time()
    t_wall = time.time()

    log.info("Controller + dithering running for %d s ...", RUN_DURATION)

    while True:
        tick_start = loop.time()
        elapsed    = tick_start - t0
        if elapsed >= RUN_DURATION:
            break

        # 1. Proxy
        rpo_hat = await asyncio.to_thread(read_proxy_redis, r)

        # 2. PI tick
        dwb   = ctrl.tick(rpo_hat)
        error = cfg.rpo_star - rpo_hat

        # 3. Dithering actuator
        mode = await asyncio.to_thread(dither.tick, dwb, r)

        ticks.append({
            "t":       round(elapsed, 3),
            "rpo_hat": round(rpo_hat, 4),
            "error":   round(error, 4),
            "dwb":     round(dwb, 4),
            "mode":    mode,
            "eff_if":  round(dither.effective_if, 4),
        })

        sleep_s = max(0.0, TS - (loop.time() - tick_start))
        await asyncio.sleep(sleep_s)

    workload.stop()
    r.close()
    await asyncio.sleep(COOLDOWN)

    # Save
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "engine":      "redis_dither",
        "run_id":      run_id,
        "tps":         tps,
        "rpo_star":    cfg.rpo_star,
        "kp":          cfg.kp,
        "ki":          cfg.ki,
        "duration_s":  RUN_DURATION,
        "wall_time_s": round(time.time() - t_wall, 1),
        "n_ticks":     len(ticks),
        "ticks":       ticks,
    }
    fname = out_dir / f"redis_dither_run{run_id:02d}_{int(tps)}tps.json"
    fname.write_text(json.dumps(record, indent=2))
    log.info("Saved → %s", fname)
    return fname


async def run_campaign(n_runs: int, start_run: int, out_dir: Path):
    rng   = random.Random(99)
    total = n_runs
    t0    = time.time()

    for i, run_id in enumerate(range(start_run, start_run + n_runs)):
        if run_id <= 5:
            tps = float(TPS_ORIGINAL[run_id - 1])
        else:
            tps = float(rng.randint(*TPS_RANGE))

        eta = (time.time() - t0) / max(i, 1) * (total - i)
        log.info("Progress: %d/%d  ETA: %.0f min", i + 1, total, eta / 60)

        await run_dithering_experiment(tps, run_id, out_dir)
        log.info("Pausing %d s ...", INTER_RUN_PAUSE)
        await asyncio.sleep(INTER_RUN_PAUSE)

    log.info("Dithering campaign done — %.1f min", (time.time() - t0) / 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-runs",    type=int, default=20)
    parser.add_argument("--start-run", type=int, default=1)
    parser.add_argument("--out-dir",   type=Path, default=Path("results"))
    args = parser.parse_args()

    asyncio.run(run_campaign(args.n_runs, args.start_run, args.out_dir))
