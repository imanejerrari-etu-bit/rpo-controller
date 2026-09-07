"""
Engine Actuators — Section IV-B of the paper.

MongoDB:   setParameter journalCommitInterval = max(1, min(500, ⌊dwb×1000⌋)) ms
MySQL PXC: SET GLOBAL innodb_flush_log_at_trx_commit = v
           v=1 if dwb < 5 s, v=2 if dwb < 30 s, else v=0
Redis:     CONFIG SET appendfsync {always|everysec|no}
           always if dwb < 1 s, everysec if dwb < 10 s, else no
"""
from __future__ import annotations
import logging
import time

from rpo_controller.config import (
    REDIS_THRESH_ALWAYS, REDIS_THRESH_EVERYSEC,
    MYSQL_THRESH_V1, MYSQL_THRESH_V2,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# MongoDB actuator
# ─────────────────────────────────────────────────────────────────────────────

def actuate_mongodb(db, dwb: float) -> int:
    """
    Apply write-behind interval to MongoDB.

    Returns:
        j_ms: applied journalCommitInterval (ms)
    """
    j_ms = max(1, min(500, int(dwb * 1000)))
    t0   = time.perf_counter()
    db.command({"setParameter": 1, "journalCommitInterval": j_ms})
    latency_ms = (time.perf_counter() - t0) * 1000
    log.debug("MongoDB actuator: JCI=%d ms  latency=%.1f ms", j_ms, latency_ms)
    return j_ms


# ─────────────────────────────────────────────────────────────────────────────
# MySQL PXC actuator
# ─────────────────────────────────────────────────────────────────────────────

def actuate_mysql(conn, dwb: float) -> int:
    """
    Apply innodb_flush_log_at_trx_commit.

    Mapping (paper thresholds at 5 s and 30 s):
        dwb < 5  s  → v=1  (flush on every commit, RPO ≈ 0)
        dwb < 30 s  → v=2  (log written on commit, flushed per second)
        dwb ≥ 30 s  → v=0  (log flushed once per second, least durable)

    Note: command is replicated to all three Galera nodes → median 343 ms.
    """
    if dwb < MYSQL_THRESH_V1:
        v = 1
    elif dwb < MYSQL_THRESH_V2:
        v = 2
    else:
        v = 0

    t0     = time.perf_counter()
    cursor = conn.cursor()
    cursor.execute(f"SET GLOBAL innodb_flush_log_at_trx_commit = {v}")
    conn.commit()
    cursor.close()
    latency_ms = (time.perf_counter() - t0) * 1000
    log.debug("MySQL actuator: v=%d  latency=%.1f ms", v, latency_ms)
    return v


# ─────────────────────────────────────────────────────────────────────────────
# Redis actuator
# ─────────────────────────────────────────────────────────────────────────────

def actuate_redis(r, dwb: float) -> str:
    """
    Apply appendfsync mode to Redis.

    Mapping (paper thresholds at 1 s and 10 s):
        dwb ∈ [0, 1)   → always    (I_f ≈ 0.001 s)
        dwb ∈ [1, 10)  → everysec  (I_f = 1.0 s)
        dwb ∈ [10, ∞)  → no        (I_f ≈ 30 s, OS-driven)
    """
    if dwb < REDIS_THRESH_ALWAYS:
        mode = "always"
    elif dwb < REDIS_THRESH_EVERYSEC:
        mode = "everysec"
    else:
        mode = "no"

    t0 = time.perf_counter()
    r.config_set("appendfsync", mode)
    latency_ms = (time.perf_counter() - t0) * 1000
    log.debug("Redis actuator: appendfsync=%s  latency=%.1f ms", mode, latency_ms)
    return mode
