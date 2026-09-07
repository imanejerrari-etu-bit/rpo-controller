"""
Ablation Study - MongoDB, 250 TPS, 5 runs x 4 variants x 600s

Isolates the contribution of each PI controller mechanism:
  - P-only     (Ki = 0)
  - I-only     (Kp = 0)
  - No AW      (anti-windup disabled: I_max set very large)
  - No DB      (deadband disabled: epsilon_d = 0)
  - Full PI    (baseline -- already have this data, not re-run here)

Usage:
    python run_ablation.py --variant p_only
    python run_ablation.py --variant i_only
    python run_ablation.py --variant no_antiwindup
    python run_ablation.py --variant no_deadband
    python run_ablation.py --variant all        # runs all 4 sequentially
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

from rpo_controller.config import (
    TS, ENGINES, EngineConfig,
    MONGO_URI, I_MAX, DWB_MIN, DWB_MAX,
)
from rpo_controller.pi_controller import PIController
from rpo_controller.proxies import read_proxy_mongodb
from rpo_controller.actuators import actuate_mongodb
from experiments.workload import MongoWorkload

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger("ablation")

TPS = 250
RUN_DURATION = 600
WARMUP = 10
COOLDOWN = 15
N_RUNS = 5

# Baseline MongoDB config (Regime 2, active tracking)
BASE_RPO_STAR = 0.5
BASE_KP = 0.8
BASE_KI = 0.20

VARIANTS = {
    "p_only": dict(kp=BASE_KP, ki=0.0, deadband=None, i_max=I_MAX,
                   desc="Proportional-only (Ki=0)"),
    "heuristic_pid": dict(kp=0.3, ki=0.05, deadband=None, i_max=I_MAX,
                          desc="Heuristic PID (non-ITAE-tuned gains)"),
    "i_only": dict(kp=0.0, ki=BASE_KI, deadband=None, i_max=I_MAX,
                   desc="Integral-only (Kp=0)"),
    "no_antiwindup": dict(kp=BASE_KP, ki=BASE_KI, deadband=None, i_max=1e6,
                           desc="PI without anti-windup (I_max -> very large)"),
    "no_deadband": dict(kp=BASE_KP, ki=BASE_KI, deadband=0.0, i_max=I_MAX,
                         desc="PI without deadband (epsilon_d=0)"),
}


async def _run_one(variant_name: str, run_id: int, out_dir: Path):
    v = VARIANTS[variant_name]
    log.info("=" * 60)
    log.info("ABLATION  variant=%s (%s)  run=%d/%d",
              variant_name, v["desc"], run_id, N_RUNS)
    log.info("=" * 60)

    # Build a custom EngineConfig for this variant
    cfg = EngineConfig(name="mongodb", rpo_star=BASE_RPO_STAR,
                       kp=v["kp"], ki=v["ki"])
    if v["deadband"] is not None:
        cfg.deadband = v["deadband"]

    # Reset MongoDB state
    client = pymongo.MongoClient(MONGO_URI)
    client["admin"].command({"setParameter": 1, "journalCommitInterval": 100})
    client["workload"]["writes"].drop()
    mongo_db = client["admin"]

    # Start original token-bucket workload
    workload = MongoWorkload(TPS)
    workload.start()
    log.info("Workload started (%d s warmup)...", WARMUP)
    await asyncio.sleep(WARMUP)

    # Custom control loop (mirrors service.py's _run_engine_loop,
    # but with variant-specific gains / anti-windup / deadband)
    ctrl = PIController(cfg, ts=TS, i_max=v["i_max"],
                        dwb_min=DWB_MIN, dwb_max=DWB_MAX)
    ticks = []
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    log.info("Controller running for %d s ...", RUN_DURATION)

    while True:
        tick_start = loop.time()
        elapsed = tick_start - t0
        if elapsed >= RUN_DURATION:
            break
        try:
            rpo_hat = await asyncio.to_thread(read_proxy_mongodb, mongo_db)
        except Exception as exc:
            log.warning("proxy error: %s", exc)
            rpo_hat = cfg.rpo_star
        dwb = ctrl.tick(rpo_hat)
        error = cfg.rpo_star - rpo_hat
        try:
            val = await asyncio.to_thread(actuate_mongodb, mongo_db, dwb)
        except Exception as exc:
            log.warning("actuator error: %s", exc)
            val = None
        ticks.append({"t": round(elapsed, 3), "rpo_hat": round(rpo_hat, 4),
                      "error": round(error, 4), "dwb": round(dwb, 4),
                      "act": str(val)})
        sleep_s = max(0.0, TS - (loop.time() - tick_start))
        await asyncio.sleep(sleep_s)

    log.info("Loop finished -- %d ticks collected", len(ticks))
    workload.stop()
    log.info("Cooldown %d s ...", COOLDOWN)
    await asyncio.sleep(COOLDOWN)
    client.close()

    record = {
        "engine": "mongodb", "variant": variant_name,
        "variant_desc": v["desc"], "run_id": run_id, "tps": TPS,
        "rpo_star": cfg.rpo_star, "kp": cfg.kp, "ki": cfg.ki,
        "deadband": cfg.deadband, "i_max": v["i_max"],
        "duration_s": RUN_DURATION, "n_ticks": len(ticks), "ticks": ticks,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"ablation_{variant_name}_run{run_id:02d}.json"
    fname.write_text(json.dumps(record, indent=2))
    log.info("Saved -> %s", fname)


async def main(variant: str, out_dir: Path):
    variants_to_run = list(VARIANTS.keys()) if variant == "all" else [variant]
    for v in variants_to_run:
        for run_id in range(1, N_RUNS + 1):
            try:
                await _run_one(v, run_id, out_dir)
            except Exception as exc:
                log.error("Run failed: %s -- continuing", exc)
            await asyncio.sleep(10)
    log.info("=== Ablation study complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", required=True,
                        choices=list(VARIANTS.keys()) + ["all"])
    parser.add_argument("--out-dir", default="results_ablation", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.variant, args.out_dir))
