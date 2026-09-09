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
# NOTE: these two are kept for backward compatibility (used as defaults by
# PIController/baseline controllers' constructor signatures, and directly by
# run_ablation.py which is MongoDB-only). They match MongoDB's actual
# actuator ceiling. DO NOT rely on these alone for MySQL/Redis -- use
# ENGINE_DWB_BOUNDS below instead (Reviewer 1, point 4 fix).
DWB_MIN: float = 0.001   # 1 ms
DWB_MAX: float = 0.500   # 500 ms  (MongoDB upper limit)

# ── Per-engine actuator bounds (Reviewer 1, point 4 fix) ─────────────────────
# ROOT CAUSE this fixes: PIController(cfg) was being called without
# per-engine dwb_min/dwb_max, so every engine silently inherited
# DWB_MAX=0.500 above -- meaning the controller could never command a
# value above 500ms, making MySQL PXC's v=1 threshold (5s) / v=0
# threshold (30s), and Redis's "no" threshold (10s), unreachable dead
# code in the actuator logic.
DWB_MIN_MONGODB: float = 0.001   # 1 ms -- unchanged, matches actual ceiling
DWB_MAX_MONGODB: float = 0.500   # 500 ms

DWB_MIN_MYSQL: float = 0.001
DWB_MAX_MYSQL: float = 35.0      # must exceed MYSQL_THRESH_V2 (30s) below

DWB_MIN_REDIS: float = 0.001
DWB_MAX_REDIS: float = 35.0      # must exceed REDIS_THRESH_EVERYSEC (10s) below

ENGINE_DWB_BOUNDS = {
    "mongodb": (DWB_MIN_MONGODB, DWB_MAX_MONGODB),
    "mysql":   (DWB_MIN_MYSQL,   DWB_MAX_MYSQL),
    "redis":   (DWB_MIN_REDIS,   DWB_MAX_REDIS),
}

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

# ── MySQL PXC per-node access (Reviewer 1, point 7 fix) ──────────────────────
# ROOT CAUSE this fixes: a single mysql.connector connection through a
# load-balanced Kubernetes Service is guaranteed to stick to ONE backend
# pod for its entire lifetime. Since innodb_flush_log_at_trx_commit is a
# per-node session/global variable (NOT replicated by Galera), applying
# it through one such connection only ever tunes one node -- which may
# not even be the node the workload generator's own (separately
# load-balanced) connection happens to be writing to.
#
# ADJUST THESE to match whichever PXC deployment you actually use:
#   - k8s/mysql-pxc.yaml (plain StatefulSet, currently in the repo):
#       MYSQL_PXC_HEADLESS_SVC = "mysql-pxc-headless"
#       MYSQL_PXC_POD_PREFIX   = "mysql-pxc"
#   - Percona Operator deployment (pxc_cr.yaml, used for tonight's
#     fault-injection testing, includes real HAProxy):
#       MYSQL_PXC_HEADLESS_SVC = "pxc-rpo-experiment-pxc"
#       MYSQL_PXC_POD_PREFIX   = "pxc-rpo-experiment-pxc"
MYSQL_PXC_REPLICAS: int = 3
MYSQL_PXC_HEADLESS_SVC: str = "mysql-pxc-headless"
MYSQL_PXC_POD_PREFIX: str = "mysql-pxc"


def mysql_pxc_pod_hosts():
    """
    Stable per-pod DNS names, one per Galera node.

    Only resolves from INSIDE the cluster's DNS (i.e. if the controller
    itself runs as a pod). If running the controller from outside the
    cluster (e.g. a Windows host via kubectl port-forward, as used for
    manual testing), you cannot use these hostnames directly -- instead
    open one port-forward PER POD on distinct local ports and return
    that list of (host, port) pairs from an equivalent function. See
    mysql_multinode_patch_point7.py for the full discussion.
    """
    return [f"{MYSQL_PXC_POD_PREFIX}-{i}.{MYSQL_PXC_HEADLESS_SVC}"
            for i in range(MYSQL_PXC_REPLICAS)]


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
