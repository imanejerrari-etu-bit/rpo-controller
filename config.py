"""
Configuration — all constants taken directly from the paper.
"""
from dataclasses import dataclass, field
from typing import Dict


# ── Sampling period (s) ─────────────────────────────────────────────────────
TS: float = 0.5          # 500 ms

# ── Anti-windup integral clamp ───────────────────────────────────────────────
I_MAX: float = 10.0      # ±10 s (not specified in paper; conservative choice)

# ── Actuator limits (s) ──────────────────────────────────────────────────────
DWB_MIN: float = 0.001   # 1 ms
DWB_MAX: float = 0.500   # 500 ms  (MongoDB upper limit)

# ── WiredTiger checkpoint interval (s) used in MongoDB proxy ─────────────────
ALPHA_MONGO: float = 10.0          # Eq. 4: α = 10 s
MONGO_PROXY_MAX: float = 35.0      # clamp ceiling for MongoDB proxy

# ── Redis AOF fsync-interval mapping ─────────────────────────────────────────
REDIS_FSYNC_MAP: Dict[str, float] = {
    "always":  0.001,   # ≈ 0 s  (synchronous)
    "everysec": 1.0,    # 1 s
    "no":      30.0,    # OS-driven, ≈ 30 s
}
# Thresholds for Redis actuator (dwb → appendfsync mode)
REDIS_THRESH_ALWAYS:   float = 1.0   # dwb < 1 s  → always
REDIS_THRESH_EVERYSEC: float = 10.0  # dwb < 10 s → everysec; else → no

# ── MySQL PXC actuator thresholds (dwb → innodb_flush_log_at_trx_commit) ─────
MYSQL_THRESH_V1: float = 5.0    # dwb < 5 s  → v=1 (flush on commit)
MYSQL_THRESH_V2: float = 30.0   # dwb < 30 s → v=2 (flush per second)
# dwb ≥ 30 s → v=0 (least durable)

# ── MySQL proxy weights (Eq. 5) ──────────────────────────────────────────────
MYSQL_W_QUEUE:   float = 0.5    # wsrep_local_send_queue
MYSQL_W_THREADS: float = 0.3    # Threads_running
MYSQL_W_WAITS:   float = 0.1    # Innodb_log_waits


@dataclass
class EngineConfig:
    """Per-engine PI gains and RPO target (Table 3 in paper)."""
    name:       str
    rpo_star:   float          # SLA target (s)
    kp:         float          # Proportional gain
    ki:         float          # Integral gain
    deadband:   float = field(init=False)  # ε_d = 0.05 × RPO*

    def __post_init__(self):
        self.deadband = 0.05 * self.rpo_star


# ── Gains from ITAE grid search (Table 3) ────────────────────────────────────
ENGINES: Dict[str, EngineConfig] = {
    "mongodb": EngineConfig(name="mongodb",   rpo_star=0.5, kp=0.8, ki=0.20),
    "mysql":   EngineConfig(name="mysql",     rpo_star=1.0, kp=0.5, ki=0.10),
    "redis":   EngineConfig(name="redis",     rpo_star=1.0, kp=0.4, ki=0.08),
}

# ── Connection defaults (override via env vars in practice) ──────────────────
MYSQL_HOST:  str = "localhost"
MYSQL_USER:  str = "root"
MONGO_URI:   str = "mongodb://localhost:27017/admin"
MYSQL_PORT:  int = 3307
MYSQL_PASS:  str = "TestPass123"
MYSQL_DB:    str = "ycsb"
REDIS_HOST:  str = "localhost"
REDIS_PORT:  int = 6379
REDIS_PASS:  str = ""          # empty = no auth

# ── Experiment defaults ───────────────────────────────────────────────────────
RUN_DURATION:  int = 600    # seconds
WARMUP:        int = 10     # seconds — steady-state starts after this
COOLDOWN:      int = 15     # seconds
SS_START:      int = 300    # steady-state window start (t > 300 s)
BOOTSTRAP_N:   int = 2000   # bootstrap resamples for CI
TOLERANCE:     float = 0.10 # 10% tolerance band for convergence time
