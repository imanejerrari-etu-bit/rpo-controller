"""
RPO Proxy Measurements — Section III-B of the paper.

Each proxy is an observable monotone lower bound on the true RPO
(which would require a crash-recovery test to measure directly).

Eq. 4  — MongoDB:  rpo_hat = J/1000 + α·ρ,  α = 10 s
Eq. 5  — MySQL:    rpo_hat = 0.5·Q_ws + 0.3·τ_r + 0.1·W_l
Eq. 6  — Redis:    rpo_hat = clamp(I_f + 0.1·d_f + 0.5·(g-1), I_f, 30)
"""
from __future__ import annotations
import time
import logging
from typing import Optional

from rpo_controller.config import (
    ALPHA_MONGO, MONGO_PROXY_MAX,
    REDIS_FSYNC_MAP,
    MYSQL_W_QUEUE, MYSQL_W_THREADS, MYSQL_W_WAITS,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# MongoDB proxy (Eq. 4)
# ─────────────────────────────────────────────────────────────────────────────

def read_proxy_mongodb(db) -> float:
    """
    rpo_hat_mg(t) = J/1000 + α·ρ  clamped to [J/1000, 35] s

    Args:
        db: pymongo database object (admin db)

    Returns:
        RPO proxy in seconds
    """
    try:
        status = db.command("serverStatus")
        wt     = status["wiredTiger"]

        # Journal commit interval (ms) — the parameter we are controlling
        j_ms = status.get("journalCommitInterval",
                          db.command({"getParameter": 1,
                                      "journalCommitInterval": 1})
                          .get("journalCommitInterval", 100))
        j_s  = float(j_ms) / 1000.0

        # WiredTiger dirty-page ratio ρ ∈ [0, 1]
        cache      = wt["cache"]
        dirty_bytes = cache.get("tracked dirty bytes in the cache", 0)
        max_bytes   = cache.get("maximum bytes configured", 1)
        rho = min(1.0, dirty_bytes / max(max_bytes, 1))

        proxy = j_s + ALPHA_MONGO * rho
        proxy = max(j_s, min(MONGO_PROXY_MAX, proxy))
        return proxy

    except Exception as exc:
        log.warning("MongoDB proxy read failed: %s", exc)
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# MySQL PXC proxy (Eq. 5)
# ─────────────────────────────────────────────────────────────────────────────

def read_proxy_mysql(conn) -> float:
    """
    rpo_hat_my(t) = 0.5·Q_ws + 0.3·τ_r + 0.1·W_l

    Q_ws = wsrep_local_send_queue
    τ_r  = Threads_running
    W_l  = Innodb_log_waits

    Args:
        conn: mysql.connector connection object

    Returns:
        RPO proxy in seconds
    """
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW GLOBAL STATUS WHERE Variable_name IN "
                       "('wsrep_local_send_queue', 'Threads_running', "
                       "'Innodb_log_waits')")
        rows = {row[0]: float(row[1]) for row in cursor.fetchall()}
        cursor.close()

        q_ws  = rows.get("wsrep_local_send_queue", 0.0)
        tau_r = rows.get("Threads_running", 0.0)
        w_l   = rows.get("Innodb_log_waits", 0.0)

        proxy = (MYSQL_W_QUEUE   * q_ws
               + MYSQL_W_THREADS * tau_r
               + MYSQL_W_WAITS   * w_l)
        return max(0.0, proxy)

    except Exception as exc:
        log.warning("MySQL proxy read failed: %s", exc)
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Redis proxy (Eq. 6)
# ─────────────────────────────────────────────────────────────────────────────

class RedisProxyState:
    """Tracks per-run baseline AOF size for growth ratio g."""
    def __init__(self):
        self.aof_base: Optional[int] = None
        self.last_fsync_time: float  = time.time()

    def reset(self):
        self.aof_base       = None
        self.last_fsync_time = time.time()


_redis_state = RedisProxyState()


def reset_redis_proxy():
    """Call at the start of each run (after BGREWRITEAOF)."""
    _redis_state.reset()


def read_proxy_redis(r) -> float:
    """
    rpo_hat_rd(t) = clamp(I_f + 0.1·d_f + 0.5·(g−1), I_f, 30) s

    I_f = fsync interval from appendfsync mode
    d_f ≈ I_f (upper bound; Redis does not expose exact last-fsync timestamp)
    g   = aof_current_size / aof_base_size  (growth ratio)

    Args:
        r: redis.Redis client object

    Returns:
        RPO proxy in seconds (clamped)
    """
    try:
        info = r.info("persistence")

        # ── Fsync interval I_f ────────────────────────────────────────────────
        mode = info.get("aof_enabled", 0)
        if not mode:
            return 30.0    # AOF disabled → worst case

        appendfsync = r.config_get("appendfsync").get("appendfsync", "everysec")
        i_f = REDIS_FSYNC_MAP.get(appendfsync, 1.0)

        # ── AOF growth ratio g ────────────────────────────────────────────────
        cur_size  = info.get("aof_current_size", 0)
        base_size = info.get("aof_base_size", cur_size)

        if _redis_state.aof_base is None:
            # First tick of this run — record baseline
            _redis_state.aof_base = max(base_size, 1)

        g = cur_size / max(_redis_state.aof_base, 1)

        # ── d_f: time since last fsync ────────────────────────────────────────
        # Approximated as I_f (conservative upper bound per mode)
        d_f = i_f

        raw   = i_f + 0.1 * d_f + 0.5 * (g - 1.0)
        proxy = max(i_f, min(30.0, raw))
        return proxy

    except Exception as exc:
        log.warning("Redis proxy read failed: %s", exc)
        return 1.0
