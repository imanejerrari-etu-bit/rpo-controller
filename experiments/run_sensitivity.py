"""
MySQL Proxy Sensitivity Analysis — Point 2.

Tests 9 weight configurations (±50% variation on each weight)
to quantify sensitivity of the violation rate to proxy calibration.

Base weights: w_q=0.5, w_t=0.3, w_l=0.1
Variations: each weight × {0.5, 1.0, 1.5} (others fixed)

Usage:
    python run_sensitivity.py
    python run_sensitivity.py --n-runs 3   # 3 runs per config
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from itertools import product

sys.path.insert(0, str(Path(__file__).parent.parent))

import mysql.connector

from rpo_controller import config as cfg_module
from rpo_controller.config import (
    TS, ENGINES, RUN_DURATION, WARMUP, COOLDOWN,
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASS, MYSQL_DB,
    SS_START,
)
from rpo_controller.pi_controller import PIController
from rpo_controller.proxies import read_proxy_mysql
from rpo_controller.actuators import actuate_mysql
from experiments.workload import make_workload
from experiments.run_experiment import _setup_mysql

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("sensitivity")

# Base weights
W_BASE = {"w_q": 0.5, "w_t": 0.3, "w_l": 0.1}
SCALE_FACTORS = [0.5, 1.0, 1.5]
# TPS fixed at median of campaign range
SENSITIVITY_TPS = 80.0
INTER_RUN_PAUSE = 30


def build_configs():
    """Generate 9 sensitivity configurations."""
    configs = []
    for param, scales in [
        ("w_q", SCALE_FACTORS),
        ("w_t", SCALE_FACTORS),
        ("w_l", SCALE_FACTORS),
    ]:
        for scale in scales:
            w = W_BASE.copy()
            w[param] = round(W_BASE[param] * scale, 3)
            configs.append({
                "label":  f"{param}_x{scale}",
                "w_q": w["w_q"],
                "w_t": w["w_t"],
                "w_l": w["w_l"],
            })
    return configs


async def run_one(config: dict, run_id: int, out_dir: Path) -> dict:
    """Run one sensitivity experiment with given proxy weights."""
    cfg  = ENGINES["mysql"]
    ctrl = PIController(cfg)
    ticks = []

    # Override proxy weights globally
    cfg_module.MYSQL_W_QUEUE   = config["w_q"]
    cfg_module.MYSQL_W_THREADS = config["w_t"]
    cfg_module.MYSQL_W_WAITS   = config["w_l"]

    log.info("Config %s  w=(%.2f, %.2f, %.2f)",
             config["label"], config["w_q"],
             config["w_t"], config["w_l"])

    _setup_mysql()

    conn = mysql.connector.connect(
        host=MYSQL_HOST, port=MYSQL_PORT,
        user=MYSQL_USER, password=MYSQL_PASS,
        database=MYSQL_DB, autocommit=True,
    )

    workload = make_workload("mysql", SENSITIVITY_TPS)
    workload.start()
    await asyncio.sleep(WARMUP)

    loop = asyncio.get_running_loop()
    t0   = loop.time()

    while True:
        tick_start = loop.time()
        elapsed    = tick_start - t0
        if elapsed >= RUN_DURATION:
            break

        rpo_hat = await asyncio.to_thread(read_proxy_mysql, conn)
        dwb     = ctrl.tick(rpo_hat)
        val     = await asyncio.to_thread(actuate_mysql, conn, dwb)

        ticks.append({
            "t":       round(elapsed, 3),
            "rpo_hat": round(rpo_hat, 4),
            "dwb":     round(dwb, 4),
            "act":     val,
        })

        sleep_s = max(0.0, TS - (loop.time() - tick_start))
        await asyncio.sleep(sleep_s)

    workload.stop()
    conn.close()
    await asyncio.sleep(COOLDOWN)

    # Compute stats
    import numpy as np
    rpo_vals = np.array([tk["rpo_hat"] for tk in ticks])
    t_vals   = np.array([tk["t"]       for tk in ticks])
    ss_mask  = t_vals > SS_START
    ss_rpo   = rpo_vals[ss_mask]
    viol_pct = float(np.mean(rpo_vals > cfg.rpo_star)) * 100

    record = {
        "config":   config,
        "run_id":   run_id,
        "viol_pct": round(viol_pct, 2),
        "ss_mu":    round(float(np.mean(ss_rpo)), 4) if len(ss_rpo) else 0,
        "ss_sigma": round(float(np.std(ss_rpo)),  4) if len(ss_rpo) else 0,
        "ticks":    ticks,
    }

    label = config["label"]
    fname = out_dir / f"sensitivity_{label}_run{run_id:02d}.json"
    fname.write_text(json.dumps(record, indent=2))
    log.info("  viol=%.1f%%  ss_mu=%.3fs → %s", viol_pct, record["ss_mu"], fname)
    return record


async def run_sensitivity(n_runs: int, out_dir: Path):
    configs = build_configs()
    out_dir.mkdir(parents=True, exist_ok=True)
    all_results = []

    for cfg in configs:
        run_results = []
        for run_id in range(1, n_runs + 1):
            r = await run_one(cfg, run_id, out_dir)
            run_results.append(r)
            await asyncio.sleep(INTER_RUN_PAUSE)
        all_results.append((cfg, run_results))

    # Reset to base weights
    cfg_module.MYSQL_W_QUEUE   = W_BASE["w_q"]
    cfg_module.MYSQL_W_THREADS = W_BASE["w_t"]
    cfg_module.MYSQL_W_WAITS   = W_BASE["w_l"]

    # Print summary table
    print("\n" + "=" * 65)
    print("MySQL Proxy Sensitivity Analysis")
    print(f"{'Config':<15} {'w_q':>5} {'w_t':>5} {'w_l':>5} "
          f"{'Viol% mean':>10} {'Viol% std':>10}")
    print("-" * 65)
    import numpy as np
    for cfg, runs in all_results:
        viols = [r["viol_pct"] for r in runs]
        print(f"{cfg['label']:<15} {cfg['w_q']:>5.2f} {cfg['w_t']:>5.2f} "
              f"{cfg['w_l']:>5.2f} {np.mean(viols):>10.2f} "
              f"{np.std(viols):>10.2f}")
    print("=" * 65)

    # Save summary
    summary_path = out_dir / "sensitivity_summary.json"
    summary_path.write_text(json.dumps([
        {"config": c, "runs": [{"viol_pct": r["viol_pct"],
                                "ss_mu": r["ss_mu"]} for r in rs]}
        for c, rs in all_results
    ], indent=2))
    log.info("Summary → %s", summary_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-runs", type=int, default=3,
                        help="Runs per config (default: 3)")
    parser.add_argument("--out-dir", type=Path,
                        default=Path("results/sensitivity"))
    args = parser.parse_args()
    asyncio.run(run_sensitivity(args.n_runs, args.out_dir))
