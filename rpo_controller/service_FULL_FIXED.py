"""
Controller Service — Section IV-A of the paper.

Python 3.13 asyncio service with three concurrent control loops at
T_s = 500 ms. Each loop runs independently per engine.
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

import pymongo
import mysql.connector
import redis as redis_lib

from rpo_controller.config import (
    TS, ENGINES, EngineConfig, ENGINE_DWB_BOUNDS,
    MONGO_URI, MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASS, MYSQL_DB,
    REDIS_HOST, REDIS_PORT, REDIS_PASS,
    mysql_pxc_pod_hosts,
)
from rpo_controller.pi_controller import PIController
from rpo_controller.mpc_controller import MPCController
from rpo_controller.predictors import MovingAveragePredictor, ARIMAPredictor
from rpo_controller.proxies import (
    read_proxy_mongodb, read_proxy_mysql, read_proxy_redis,
    reset_redis_proxy,
)
from rpo_controller.actuators import actuate_mongodb, actuate_mysql, actuate_redis

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tick record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Tick:
    t:       float    # elapsed time (s)
    rpo_hat: float    # proxy measurement (s)
    error:   float    # e[k] = RPO* - rpo_hat
    dwb:     float    # applied write-behind interval (s)
    actuator_val: Any # raw value sent to engine (j_ms / v / mode)


# ─────────────────────────────────────────────────────────────────────────────
# Controller factory (PATCH -- Reviewer 1, point 4: per-engine dwb bounds)
# ─────────────────────────────────────────────────────────────────────────────

def _make_controller(cfg: EngineConfig, controller_type: str, predictor_type: str):
    """
    Build a controller instance from CLI-friendly string names.

    controller_type: "pi" | "mpc"
    predictor_type:  "ma" | "arima"   (ignored when controller_type == "pi")
    """
    dwb_min, dwb_max = ENGINE_DWB_BOUNDS[cfg.name]   # PATCH (point 4 fix)

    if controller_type == "pi":
        return PIController(cfg, dwb_min=dwb_min, dwb_max=dwb_max)

    if controller_type == "mpc":
        if predictor_type == "ma":
            predictor = MovingAveragePredictor()
        elif predictor_type == "persistence":
            predictor = MovingAveragePredictor(use_trend=False)
        elif predictor_type == "arima":
            predictor = ARIMAPredictor()
        else:
            raise ValueError(f"Unknown predictor_type: {predictor_type!r}")
        return MPCController(cfg, predictor, dwb_min=dwb_min, dwb_max=dwb_max)

    raise ValueError(f"Unknown controller_type: {controller_type!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Per-engine async loop
# ─────────────────────────────────────────────────────────────────────────────

async def _run_engine_loop(
    engine_name: str,
    duration:    int,
    connections: Dict,
    stop_event:  asyncio.Event,
    controller_type: str = "pi",
    predictor_type:  str = "ma",
) -> List[Tick]:
    """
    Single control loop for one engine.

    Returns list of Tick records (one per 500 ms sample).
    """
    cfg   = ENGINES[engine_name]
    ctrl  = _make_controller(cfg, controller_type, predictor_type)
    ticks: List[Tick] = []
    loop  = asyncio.get_running_loop()
    t0    = loop.time()

    if engine_name == "redis":
        reset_redis_proxy()

    log.info("[%s] Loop started — controller=%s predictor=%s RPO* = %.1f s, Kp=%.2f, Ki=%.2f",
             engine_name, controller_type, predictor_type, cfg.rpo_star, cfg.kp, cfg.ki)

    while not stop_event.is_set():
        tick_start = loop.time()
        elapsed    = tick_start - t0
        if elapsed >= duration:
            break

        # 1. Read proxy (synchronous DB call, run in thread pool)
        try:
            if engine_name == "mongodb":
                rpo_hat = await asyncio.to_thread(
                    read_proxy_mongodb, connections["mongo_db"])
            elif engine_name == "mysql":
                # PATCH (point 7): proxy still only needs ONE representative
                # node's status counters (wsrep_local_send_queue etc.) --
                # this part of the design was never the bug, only the
                # actuator side was. Use the first node's connection.
                rpo_hat = await asyncio.to_thread(
                    read_proxy_mysql, connections["mysql_conns"][0])
            else:  # redis
                rpo_hat = await asyncio.to_thread(
                    read_proxy_redis, connections["redis_client"])
        except Exception as exc:
            log.warning("[%s] proxy error: %s", engine_name, exc)
            rpo_hat = cfg.rpo_star   # neutral fallback

        # 2. PI tick
        dwb   = ctrl.tick(rpo_hat)
        error = cfg.rpo_star - rpo_hat

        # 3. Actuate (run in thread pool)
        try:
            if engine_name == "mongodb":
                val = await asyncio.to_thread(
                    actuate_mongodb, connections["mongo_db"], dwb)
            elif engine_name == "mysql":
                # PATCH (point 7): pass ALL node connections, not just one --
                # actuate_mysql now loops over them internally so the
                # durability setting is applied cluster-wide, not to
                # whichever single node a load-balanced connection landed on.
                val = await asyncio.to_thread(
                    actuate_mysql, connections["mysql_conns"], dwb)
            else:
                val = await asyncio.to_thread(
                    actuate_redis, connections["redis_client"], dwb)
        except Exception as exc:
            log.warning("[%s] actuator error: %s", engine_name, exc)
            val = None

        ticks.append(Tick(t=elapsed, rpo_hat=rpo_hat, error=error,
                          dwb=dwb, actuator_val=val))

        # 4. Sleep for remainder of T_s
        elapsed_tick = loop.time() - tick_start
        sleep_s      = max(0.0, TS - elapsed_tick)
        await asyncio.sleep(sleep_s)

    log.info("[%s] Loop finished — %d ticks collected", engine_name, len(ticks))
    return ticks


# ─────────────────────────────────────────────────────────────────────────────
# Connections
# ─────────────────────────────────────────────────────────────────────────────

def _open_connections(engines: List[str]) -> Dict:
    conns = {}
    if "mongodb" in engines:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        conns["mongo_client"] = client
        conns["mongo_db"]     = client["admin"]
        log.info("MongoDB connected")

    if "mysql" in engines:
        # PATCH (Reviewer 1, point 7): open ONE connection PER PXC NODE,
        # not a single connection through the load-balanced Service.
        # See mysql_pxc_pod_hosts() in config.py for the in-cluster-DNS
        # version. If running the controller from OUTSIDE the cluster
        # (e.g. a Windows host via kubectl port-forward, as used for
        # manual testing this session), replace mysql_pxc_pod_hosts()
        # with a list of (host, port) pairs, one per pod-specific
        # port-forward, e.g.:
        #   mysql_targets = [("127.0.0.1", 33061), ("127.0.0.1", 33062),
        #                     ("127.0.0.1", 33063)]
        # and adjust the loop below accordingly.
        mysql_conns = []
        for host in mysql_pxc_pod_hosts():
            mysql_conns.append(mysql.connector.connect(
                host=host, port=MYSQL_PORT,
                user=MYSQL_USER, password=MYSQL_PASS,
                database=MYSQL_DB,
                autocommit=True,
                connection_timeout=10,
            ))
        conns["mysql_conns"] = mysql_conns
        log.info("MySQL connected to %d PXC nodes individually", len(mysql_conns))

    if "redis" in engines:
        r = redis_lib.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            password=REDIS_PASS or None,
            decode_responses=True,
            socket_timeout=5,
        )
        conns["redis_client"] = r
        log.info("Redis connected")

    return conns


def _close_connections(conns: Dict):
    if "mongo_client" in conns:
        conns["mongo_client"].close()
    if "mysql_conns" in conns:
        for c in conns["mysql_conns"]:
            try:
                c.close()
            except Exception:
                pass
    if "redis_client" in conns:
        conns["redis_client"].close()


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def run_controller(
    engines:  List[str],
    duration: int,
    controller_type: str = "pi",
    predictor_type:  str = "ma",
) -> Dict[str, List[Tick]]:
    """
    Run all requested engine loops concurrently for `duration` seconds.

    Args:
        engines: engine names to control concurrently
        duration: run length in seconds
        controller_type: "pi" (default, baseline) or "mpc"
        predictor_type: "ma" or "arima" — only used when controller_type == "mpc"

    Returns:
        Dict mapping engine_name → list of Tick records
    """
    stop_event  = asyncio.Event()
    connections = _open_connections(engines)

    try:
        tasks = [
            asyncio.create_task(
                _run_engine_loop(eng, duration, connections, stop_event,
                                  controller_type, predictor_type),
                name=f"ctrl_{eng}",
            )
            for eng in engines
        ]
        results_list = await asyncio.gather(*tasks)
    finally:
        stop_event.set()
        _close_connections(connections)

    return {eng: ticks for eng, ticks in zip(engines, results_list)}
