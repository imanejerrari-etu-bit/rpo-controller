"""
service_baseline.py — parameterized version of service.run_controller
that supports selecting the controller variant (main / naive_pid /
arima_ff). Everything else — connections, proxies, actuators, Tick
schema, warmup/cooldown handling by the caller — is byte-for-byte
identical to service.py.

Import run_controller_variant() from experiments/run_experiment_baseline.py
in place of service.run_controller().
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Dict, Any

from rpo_controller.config import TS, ENGINES, ENGINE_DWB_BOUNDS
from rpo_controller.pi_controller import PIController
from rpo_controller.baseline_controllers import (
    NaiveHeuristicPIController, ArimaFeedforwardPIController,
)
from rpo_controller.proxies import (
    read_proxy_mongodb, read_proxy_mysql, read_proxy_redis,
    reset_redis_proxy,
)
from rpo_controller.actuators import actuate_mongodb, actuate_mysql, actuate_redis
# NOTE: _open_connections / _close_connections come straight from
# service.py, which already has the point 7 multi-node MySQL fix -- so
# this file inherits it automatically, no separate change needed here.
from rpo_controller.service import _open_connections, _close_connections

log = logging.getLogger(__name__)

# PATCH (Reviewer 1, point 4): pass per-engine dwb bounds via keyword
# args -- NOT positional (*ENGINE_DWB_BOUNDS[...]), since ts/i_max sit
# between cfg and dwb_min/dwb_max in these constructors' signatures;
# positional unpacking would silently land the bounds in the wrong slots.
CONTROLLER_FACTORIES = {
    "main":      lambda cfg: PIController(cfg,
                     dwb_min=ENGINE_DWB_BOUNDS[cfg.name][0],
                     dwb_max=ENGINE_DWB_BOUNDS[cfg.name][1]),
    "naive_pid": lambda cfg: NaiveHeuristicPIController(cfg,
                     dwb_min=ENGINE_DWB_BOUNDS[cfg.name][0],
                     dwb_max=ENGINE_DWB_BOUNDS[cfg.name][1]),
    "arima_ff":  lambda cfg: ArimaFeedforwardPIController(cfg,
                     dwb_min=ENGINE_DWB_BOUNDS[cfg.name][0],
                     dwb_max=ENGINE_DWB_BOUNDS[cfg.name][1]),
}


@dataclass
class Tick:
    """Identical schema to service.Tick — kept separate only so this
    module has no import-time dependency on service.Tick's dataclass
    identity (dataclasses compare by type)."""
    t: float
    rpo_hat: float
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

    # arima_ff forecasts the proxy signal itself (rpo_hat) one tick
    # ahead — no separate TPS/write-rate counter is threaded through
    # this loop, so rpo_hat's own trend is the forecasting input.
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
                # PATCH (point 7): proxy reads from one representative
                # node (unchanged design intent) -- connections["mysql_conns"]
                # is now a list (service.py's fixed _open_connections).
                rpo_hat = await asyncio.to_thread(
                    read_proxy_mysql, connections["mysql_conns"][0])
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
                # PATCH (point 7): pass the full node list, actuate_mysql
                # now loops over all of them internally.
                val = await asyncio.to_thread(
                    actuate_mysql, connections["mysql_conns"], dwb)
            else:
                val = await asyncio.to_thread(
                    actuate_redis, connections["redis_client"], dwb)
        except Exception as exc:
            log.warning("[%s/%s] actuator error: %s", engine_name,
                        controller_variant, exc)
            val = None

        ticks.append(Tick(t=elapsed, rpo_hat=rpo_hat, error=error,
                           dwb=dwb, actuator_val=val))

        elapsed_tick = loop.time() - tick_start
        sleep_s = max(0.0, TS - elapsed_tick)
        await asyncio.sleep(sleep_s)

    log.info("[%s/%s] Loop finished — %d ticks collected",
             engine_name, controller_variant, len(ticks))
    return ticks


async def run_controller_variant(
    engines: List[str],
    duration: int,
    controller_variant: str = "main",
) -> Dict[str, List[Tick]]:
    """Drop-in for service.run_controller(engines, duration), with an
    extra controller_variant argument ("main" reproduces the original
    behaviour exactly)."""
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
