"""
run_baseline_experiment.py — CLI experiment runner for the baseline
comparison (Reviewer Comment 3).

Reuses your EXACT connection, proxy, and actuator logic from
service.py / proxies.py / actuators.py — untouched. The only thing
that changes is:
  1. which controller class is instantiated per engine
     (main / naive_pid / arima_ff)
  2. a CSV row is appended per tick, in the exact format expected by
     analyze_baseline_results.py

Place this file in the rpo_controller package, next to service.py.

Usage (PowerShell), one call per (engine, controller, run):
--------------------------------------------------------------
    python -m rpo_controller.run_baseline_experiment `
        --engines mongodb --controller main      --run-id r1 --duration 600 --out results.csv

    python -m rpo_controller.run_baseline_experiment `
        --engines mongodb --controller naive_pid --run-id r1 --duration 600 --out results.csv

    python -m rpo_controller.run_baseline_experiment `
        --engines mongodb --controller arima_ff  --run-id r1 --duration 600 --out results.csv

Repeat with --run-id r2, r3, ... (recommended: 5 per engine/controller
for this revision, see RUNBOOK). All rows append to the same
results.csv, ready for analyze_baseline_results.py.

You can pass multiple --engines at once (they run concurrently, same
as service.py); a run_id must still be unique per (engine,controller)
combination if you want independent runs treated separately.
"""
from __future__ import annotations
import argparse
import asyncio
import csv
import logging
import os
from dataclasses import dataclass
from typing import List, Dict, Any

from rpo_controller.config import TS, ENGINES
from rpo_controller.pi_controller import PIController
from rpo_controller.baseline_controllers import (
    NaiveHeuristicPIController, ArimaFeedforwardPIController,
)
from rpo_controller.proxies import (
    read_proxy_mongodb, read_proxy_mysql, read_proxy_redis,
    reset_redis_proxy,
)
from rpo_controller.actuators import actuate_mongodb, actuate_mysql, actuate_redis
from rpo_controller.service import _open_connections, _close_connections

log = logging.getLogger(__name__)

CONTROLLER_FACTORIES = {
    "main":      lambda cfg: PIController(cfg),
    "naive_pid": lambda cfg: NaiveHeuristicPIController(cfg),
    "arima_ff":  lambda cfg: ArimaFeedforwardPIController(cfg),
}


@dataclass
class Tick:
    t: float
    rpo_hat: float
    rpo_star: float
    error: float
    dwb: float
    actuator_val: Any


async def _run_engine_loop(
    engine_name: str,
    controller_variant: str,
    duration: int,
    connections: Dict,
    stop_event: asyncio.Event,
) -> List[Tick]:
    cfg = ENGINES[engine_name]
    ctrl = CONTROLLER_FACTORIES[controller_variant](cfg)
    ticks: List[Tick] = []
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    # For arima_ff: forecast the proxy signal itself (rpo_hat) one tick
    # ahead, since no TPS/write-rate counter is exposed in this loop.
    # See run_baseline_experiment docstring / RUNBOOK for rationale.
    use_load_signal = (controller_variant == "arima_ff")

    if engine_name == "redis":
        reset_redis_proxy()

    log.info("[%s/%s] Loop started — RPO* = %.1f s, Kp=%.2f, Ki=%.2f",
             engine_name, controller_variant, cfg.rpo_star, cfg.kp, cfg.ki)

    while not stop_event.is_set():
        tick_start = loop.time()
        elapsed = tick_start - t0
        if elapsed >= duration:
            break

        try:
            if engine_name == "mongodb":
                rpo_hat = await asyncio.to_thread(
                    read_proxy_mongodb, connections["mongo_db"])
            elif engine_name == "mysql":
                rpo_hat = await asyncio.to_thread(
                    read_proxy_mysql, connections["mysql_conn"])
            else:
                rpo_hat = await asyncio.to_thread(
                    read_proxy_redis, connections["redis_client"])
        except Exception as exc:
            log.warning("[%s/%s] proxy error: %s", engine_name,
                        controller_variant, exc)
            rpo_hat = cfg.rpo_star

        if use_load_signal:
            dwb = ctrl.tick(rpo_hat, load_signal=rpo_hat)
        else:
            dwb = ctrl.tick(rpo_hat)
        error = cfg.rpo_star - rpo_hat

        try:
            if engine_name == "mongodb":
                val = await asyncio.to_thread(
                    actuate_mongodb, connections["mongo_db"], dwb)
            elif engine_name == "mysql":
                val = await asyncio.to_thread(
                    actuate_mysql, connections["mysql_conn"], dwb)
            else:
                val = await asyncio.to_thread(
                    actuate_redis, connections["redis_client"], dwb)
        except Exception as exc:
            log.warning("[%s/%s] actuator error: %s", engine_name,
                        controller_variant, exc)
            val = None

        ticks.append(Tick(t=elapsed, rpo_hat=rpo_hat, rpo_star=cfg.rpo_star,
                           error=error, dwb=dwb, actuator_val=val))

        elapsed_tick = loop.time() - tick_start
        sleep_s = max(0.0, TS - elapsed_tick)
        await asyncio.sleep(sleep_s)

    log.info("[%s/%s] Loop finished — %d ticks collected",
             engine_name, controller_variant, len(ticks))
    return ticks


async def run_experiment(
    engines: List[str],
    controller_variant: str,
    duration: int,
) -> Dict[str, List[Tick]]:
    stop_event = asyncio.Event()
    connections = _open_connections(engines)

    try:
        tasks = [
            asyncio.create_task(
                _run_engine_loop(eng, controller_variant, duration,
                                  connections, stop_event),
                name=f"ctrl_{eng}_{controller_variant}",
            )
            for eng in engines
        ]
        results_list = await asyncio.gather(*tasks)
    finally:
        stop_event.set()
        _close_connections(connections)

    return {eng: ticks for eng, ticks in zip(engines, results_list)}


def _append_csv(out_path: str, engine: str, controller: str, run_id: str,
                 ticks: List[Tick]) -> None:
    file_exists = os.path.isfile(out_path)
    with open(out_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["engine", "controller", "run_id", "t",
                              "rpo_star", "rpo_hat"])
        for tick in ticks:
            writer.writerow([engine, controller, run_id,
                              f"{tick.t:.3f}", tick.rpo_star, tick.rpo_hat])
    log.info("Appended %d rows to %s", len(ticks), out_path)


def main():
    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", nargs="+", required=True,
                     choices=["mongodb", "mysql", "redis"])
    ap.add_argument("--controller", required=True,
                     choices=list(CONTROLLER_FACTORIES.keys()))
    ap.add_argument("--run-id", required=True,
                     help="e.g. r1, r2, ... — must be unique per "
                          "(engine, controller) combination")
    ap.add_argument("--duration", type=int, default=600)
    ap.add_argument("--out", default="results.csv")
    args = ap.parse_args()

    results = asyncio.run(run_experiment(args.engines, args.controller,
                                          args.duration))

    for engine, ticks in results.items():
        _append_csv(args.out, engine, args.controller, args.run_id, ticks)

    print(f"\nDone. {args.out} now contains this run's ticks "
          f"(appended, not overwritten).")


if __name__ == "__main__":
    main()
