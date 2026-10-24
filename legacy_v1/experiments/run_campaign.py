"""
Automated Campaign Runner — runs 20 independent experiments per engine.

Runs 1–5 reproduce the original TPS levels from Table 4 of the paper.
Runs 6–20 use random TPS within each engine's tested range.

Usage:
    python run_campaign.py                        # all engines, 20 runs each
    python run_campaign.py --engine mongodb       # single engine
    python run_campaign.py --n-runs 20 --start-run 6   # resume from run 6
    python run_campaign.py --rpo-targets 1.0 2.0 3.0   # multi-target (MongoDB)
"""
from __future__ import annotations
import argparse
import asyncio
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from experiments.run_experiment import main as run_one

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("campaign")

# ── Original TPS levels from Table 4 in the paper ────────────────────────────
ORIGINAL_TPS = {
    #          Run1  Run2  Run3  Run4  Run5
    "mongodb": [250,  200,  300,  220,  280],
    "mysql":   [ 80,   60,  100,   70,   90],
    "redis":   [400,  300,  500,  350,  450],
}

# ── TPS ranges for additional runs ───────────────────────────────────────────
TPS_RANGE = {
    "mongodb": (180, 320),
    "mysql":   ( 55, 110),
    "redis":   (280, 520),
}

# ── Cooldown between runs (s) — allow DBs to settle ──────────────────────────
INTER_RUN_PAUSE = 30


def _tps_for_run(engine: str, run_id: int, seed: int = 42) -> float:
    """
    Return TPS for a given run number.
    Runs 1–5: original paper values.
    Runs 6+:  seeded-random within range (reproducible).
    """
    rng = random.Random(seed + run_id)
    if run_id <= 5:
        return float(ORIGINAL_TPS[engine][run_id - 1])
    lo, hi = TPS_RANGE[engine]
    return float(rng.randint(lo, hi))


async def run_campaign(
    engines:    list,
    n_runs:     int,
    start_run:  int,
    out_dir:    Path,
    rpo_targets: list = None,
):
    total = len(engines) * n_runs
    done  = 0
    t0    = time.time()

    for engine in engines:
        for run_id in range(start_run, start_run + n_runs):
            tps = _tps_for_run(engine, run_id)
            eta = (time.time() - t0) / max(done, 1) * (total - done)
            log.info("━" * 60)
            log.info("Campaign progress: %d/%d  ETA: %.0f min",
                     done + 1, total, eta / 60)

            await run_one(engine, tps, run_id, out_dir)

            done += 1
            log.info("Pausing %d s before next run ...", INTER_RUN_PAUSE)
            await asyncio.sleep(INTER_RUN_PAUSE)

    # ── Additional: MongoDB at multiple RPO targets ───────────────────────────
    if rpo_targets and "mongodb" in engines:
        from rpo_controller import config as cfg_module
        for rpo in rpo_targets:
            if rpo == 2.0:
                continue   # already covered in main campaign
            # Temporarily override RPO* for MongoDB
            cfg_module.ENGINES["mongodb"].__class__.rpo_star = rpo
            log.info("MongoDB extra target RPO* = %.1f s — 5 runs", rpo)
            for run_id in range(1, 6):
                tps = _tps_for_run("mongodb", run_id)
                tag = f"rpo{int(rpo*10):02d}"
                await run_one("mongodb", tps, run_id,
                              out_dir / f"mongodb_rpo{rpo}")
                await asyncio.sleep(INTER_RUN_PAUSE)

    log.info("=" * 60)
    log.info("Campaign complete — %d runs, %.1f min total",
             done, (time.time() - t0) / 60)
    log.info("Results in: %s", out_dir.resolve())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated 20-run campaign")
    parser.add_argument("--engine",     choices=["mongodb", "mysql", "redis"],
                        help="Single engine (default: all three)")
    parser.add_argument("--n-runs",     type=int, default=20,
                        help="Number of runs per engine (default: 20)")
    parser.add_argument("--start-run",  type=int, default=1,
                        help="First run ID (useful for resuming, default: 1)")
    parser.add_argument("--out-dir",    type=Path, default=Path("results"),
                        help="Output directory (default: results/)")
    parser.add_argument("--rpo-targets", type=float, nargs="+",
                        default=[1.0, 2.0, 3.0],
                        help="MongoDB RPO targets to test (default: 1 2 3)")
    args = parser.parse_args()

    engines = [args.engine] if args.engine else ["mongodb", "mysql", "redis"]

    asyncio.run(run_campaign(
        engines     = engines,
        n_runs      = args.n_runs,
        start_run   = args.start_run,
        out_dir     = args.out_dir,
        rpo_targets = args.rpo_targets,
    ))
