"""
Campagne complete (20 runs x 3 moteurs) avec YCSB comme generateur de charge
et le controleur PI original pour la mesure.

Reprend exactement les memes sequences de TPS que la campagne originale
et que la campagne YCSB-seule (results_mongodb/mysql/redis).

Usage:
    python run_campaign_ycsb.py --engine mongodb
    python run_campaign_ycsb.py --engine mysql
    python run_campaign_ycsb.py --engine redis
    python run_campaign_ycsb.py --engine mongodb --start-run 6   # reprise
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from experiments.run_experiment_ycsb import main as run_one

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger("campaign_ycsb")

# Sequences de TPS IDENTIQUES aux campagnes precedentes (Table 1 + memes tirages)
TPS_SEQUENCES = {
    "mongodb": [250, 200, 300, 220, 280, 236, 309, 287, 226, 205,
                191, 236, 236, 299, 212, 208, 294, 200, 211, 198],
    "mysql":   [80, 60, 100, 70, 90, 103, 63, 107, 63, 86,
                86, 63, 84, 73, 81, 98, 78, 109, 105, 84],
    "redis":   [400, 300, 500, 350, 450, 495, 482, 471, 292, 291,
                469, 495, 516, 417, 426, 454, 331, 507, 431, 309],
}


async def run_campaign(engine: str, start_run: int, n_runs: int):
    tps_seq = TPS_SEQUENCES[engine]
    out_dir = Path("results_ycsb")

    for i in range(start_run, start_run + n_runs):
        if i > 20:
            break
        tps = tps_seq[i - 1]
        log.info(">>> Campaign run %d/20 -- engine=%s tps=%d", i, engine, tps)
        try:
            await run_one(engine, float(tps), i, out_dir)
        except Exception as exc:
            log.error("Run %d FAILED: %s -- continuing with next run", i, exc)
        log.info(">>> Run %d/20 complete. Pausing 10s before next run.", i)
        await asyncio.sleep(10)

    log.info("=== Campaign complete for engine=%s ===", engine)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=["mongodb", "mysql", "redis"])
    parser.add_argument("--start-run", type=int, default=1)
    parser.add_argument("--n-runs", type=int, default=20)
    args = parser.parse_args()

    asyncio.run(run_campaign(args.engine, args.start_run, args.n_runs))
