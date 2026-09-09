"""
Token-Bucket Workload Generator — Section V-A of the paper.

MongoDB : insertMany(10 × 128 B documents)
MySQL   : INSERT into test table
Redis   : SET key value (TTL = 300 s)

Each generator runs in its own thread at a specified TPS.

--- PATCH (Reviewer 1, point 3 — fault injection ground-truth) ---
Added an optional `ack_log_path` constructor argument to each Workload
class. When set, every ACKNOWLEDGED write appends one line
"<seq_id>,<ack_unix_timestamp>\n" to that file. This is the ONLY
addition needed to support crash-recovery RPO auditing:
  - MongoDB: added a monotonic `seq_id` field per document (previously
    only had a "ts" field with no cross-batch ordering guarantee).
  - MySQL: uses the existing AUTO_INCREMENT `id` (cursor.lastrowid) —
    no schema change needed.
  - Redis: uses the existing per-write counter embedded in the key
    (`k:{counter}`) — no new field needed, just logged separately too.
Everything else is byte-for-byte identical to the original file.
"""
from __future__ import annotations
import logging
import os
import random
import string
import threading
import time
from typing import Optional

import pymongo
import mysql.connector
import redis as redis_lib

from rpo_controller.config import (
    MONGO_URI, MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASS, MYSQL_DB,
    REDIS_HOST, REDIS_PORT, REDIS_PASS,
)

log = logging.getLogger(__name__)

DOC_SIZE   = 128    # bytes per document (MongoDB)
BATCH_SIZE = 10     # docs per insertMany
REDIS_TTL  = 300    # seconds


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rand_payload(n: int) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


class TokenBucket:
    """Simple token-bucket rate limiter with a mutable rate for bursty schedules."""

    def __init__(self, rate_hz: float):
        self.rate     = rate_hz
        self.tokens   = rate_hz
        self.last_refill = time.perf_counter()

    def acquire(self):
        now     = time.perf_counter()
        elapsed = now - self.last_refill
        self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
        self.last_refill = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
        else:
            deficit = 1.0 - self.tokens
            time.sleep(deficit / self.rate)
            self.tokens = 0.0

    def set_rate(self, new_rate: float):
        """Change the target rate on the fly (used by BurstySchedule)."""
        self.rate = max(0.01, new_rate)


class _AckLogger:
    """Tiny append-only ack logger, no-op if ack_log_path is None."""

    def __init__(self, ack_log_path: Optional[str]):
        self._fh = open(ack_log_path, "a", buffering=1) if ack_log_path else None

    def log(self, seq_id: int, ack_ts: float):
        if self._fh is not None:
            self._fh.write(f"{seq_id},{ack_ts}\n")

    def close(self):
        if self._fh is not None:
            self._fh.close()


# ─────────────────────────────────────────────────────────────────────────────
# Per-engine generators
# ─────────────────────────────────────────────────────────────────────────────

class MongoWorkload:
    """insertMany at BATCH_SIZE × DOC_SIZE bytes per call."""

    def __init__(self, tps: float, ack_log_path: Optional[str] = None):
        self.tps    = tps
        self.ack_log_path = ack_log_path
        self._stop  = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seq   = 0   # monotonic doc counter (PATCH)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def set_tps(self, new_tps: float):
        """Change the target TPS on the fly (used for bursty schedules)."""
        self.tps = new_tps
        if hasattr(self, "bucket"):
            self.bucket.set_rate(new_tps / BATCH_SIZE)

    def _run(self):
        client = pymongo.MongoClient(MONGO_URI)
        coll   = client["workload"]["writes"]
        self.bucket = bucket = TokenBucket(self.tps / BATCH_SIZE)   # batches per second
        ack = _AckLogger(self.ack_log_path)
        log.info("MongoDB workload started — %.0f TPS", self.tps)

        while not self._stop.is_set():
            bucket.acquire()
            batch_seq_ids = list(range(self._seq, self._seq + BATCH_SIZE))
            self._seq += BATCH_SIZE
            docs = [{"seq_id": sid, "payload": _rand_payload(DOC_SIZE), "ts": time.time()}
                    for sid in batch_seq_ids]
            try:
                coll.insert_many(docs, ordered=False)
                ack_ts = time.time()   # batch ack: insertMany() returned successfully
                for sid in batch_seq_ids:
                    ack.log(sid, ack_ts)
            except Exception as exc:
                log.debug("MongoDB insert error: %s", exc)

        ack.close()
        client.close()
        log.info("MongoDB workload stopped")


class MysqlWorkload:
    """Simple INSERT at given TPS."""

    def __init__(self, tps: float, ack_log_path: Optional[str] = None):
        self.tps   = tps
        self.ack_log_path = ack_log_path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def set_tps(self, new_tps: float):
        """Change the target TPS on the fly (used for bursty schedules)."""
        self.tps = new_tps
        if hasattr(self, "bucket"):
            self.bucket.set_rate(new_tps)

    def _run(self):
        conn = mysql.connector.connect(
            host=MYSQL_HOST, port=MYSQL_PORT,
            user=MYSQL_USER, password=MYSQL_PASS,
            database=MYSQL_DB, autocommit=True,
        )
        cursor = conn.cursor()
        # Ensure table exists
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS writes "
            "(id BIGINT AUTO_INCREMENT PRIMARY KEY, "
            " payload VARCHAR(256), ts DOUBLE)"
        )
        self.bucket = bucket = TokenBucket(self.tps)
        ack = _AckLogger(self.ack_log_path)
        log.info("MySQL workload started — %.0f TPS", self.tps)

        while not self._stop.is_set():
            bucket.acquire()
            payload = _rand_payload(128)
            try:
                cursor.execute(
                    "INSERT INTO writes (payload, ts) VALUES (%s, %s)",
                    (payload, time.time()),
                )
                # AUTO_INCREMENT id IS the sequence id — no schema change needed (PATCH)
                ack.log(cursor.lastrowid, time.time())
            except Exception as exc:
                log.debug("MySQL insert error: %s", exc)

        ack.close()
        cursor.close()
        conn.close()
        log.info("MySQL workload stopped")


class RedisWorkload:
    """SET key value with TTL=300 s at given TPS."""

    def __init__(self, tps: float, ack_log_path: Optional[str] = None):
        self.tps   = tps
        self.ack_log_path = ack_log_path
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._counter = 0

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def set_tps(self, new_tps: float):
        """Change the target TPS on the fly (used for bursty schedules)."""
        self.tps = new_tps
        if hasattr(self, "bucket"):
            self.bucket.set_rate(new_tps)

    def _run(self):
        r      = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT,
                                  password=REDIS_PASS or None,
                                  decode_responses=True)
        self.bucket = bucket = TokenBucket(self.tps)
        ack = _AckLogger(self.ack_log_path)
        log.info("Redis workload started — %.0f TPS", self.tps)

        while not self._stop.is_set():
            bucket.acquire()
            seq   = self._counter               # PATCH: capture before increment
            key     = f"k:{seq}"
            value   = _rand_payload(64)
            self._counter += 1
            try:
                r.set(key, value, ex=REDIS_TTL)
                ack.log(seq, time.time())       # PATCH
            except Exception as exc:
                log.debug("Redis SET error: %s", exc)

        ack.close()
        r.close()
        log.info("Redis workload stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Bursty schedule wrapper (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

class BurstySchedule:
    """
    Wraps a base workload (Mongo/Mysql/Redis) and drives it through a
    step-change TPS profile: low -> burst (high) -> low. Used for the
    transient-regime experiments (Volet B) where MPC's horizon-aware
    anticipation is expected to matter most, as opposed to the
    steady-state regime (Volet A) where MPC-Persistence already
    matches PI (see paper Sec. 5.4).

    Timeline (relative to workload start, matches RUN_DURATION=600s):
        [0, burst_start)                      : low_tps
        [burst_start, burst_start+burst_dur)   : high_tps  (the burst)
        [burst_start+burst_dur, end)           : low_tps
    """

    def __init__(self, base_workload, low_tps: float, high_tps: float,
                 burst_start: float = 200.0, burst_duration: float = 60.0):
        self.base = base_workload
        self.low_tps = low_tps
        self.high_tps = high_tps
        self.burst_start = burst_start
        self.burst_duration = burst_duration
        self._stop = threading.Event()
        self._sched_thread: Optional[threading.Thread] = None

    def start(self):
        self.base.tps = self.low_tps
        self.base.start()
        self._stop.clear()
        self._sched_thread = threading.Thread(target=self._run_schedule, daemon=True)
        self._sched_thread.start()

    def stop(self):
        self._stop.set()
        if self._sched_thread:
            self._sched_thread.join(timeout=5)
        self.base.stop()

    def _run_schedule(self):
        t0 = time.perf_counter()
        burst_on = False
        while not self._stop.is_set():
            elapsed = time.perf_counter() - t0
            should_be_on = self.burst_start <= elapsed < (self.burst_start + self.burst_duration)
            if should_be_on and not burst_on:
                log.info("Bursty schedule: BURST ON (%.0f -> %.0f TPS) at t=%.1fs",
                         self.low_tps, self.high_tps, elapsed)
                self.base.set_tps(self.high_tps)
                burst_on = True
            elif not should_be_on and burst_on:
                log.info("Bursty schedule: burst OFF (-> %.0f TPS) at t=%.1fs",
                         self.low_tps, elapsed)
                self.base.set_tps(self.low_tps)
                burst_on = False
            time.sleep(0.5)


def make_bursty_workload(engine: str, low_tps: float, high_tps: float,
                          burst_start: float = 200.0, burst_duration: float = 60.0):
    base = make_workload(engine, low_tps)
    return BurstySchedule(base, low_tps, high_tps, burst_start, burst_duration)


# ─────────────────────────────────────────────────────────────────────────────
# Factory (PATCH: added optional ack_log_path passthrough)
# ─────────────────────────────────────────────────────────────────────────────

def make_workload(engine: str, tps: float, ack_log_path: Optional[str] = None):
    if engine == "mongodb":
        return MongoWorkload(tps, ack_log_path=ack_log_path)
    elif engine == "mysql":
        return MysqlWorkload(tps, ack_log_path=ack_log_path)
    elif engine == "redis":
        return RedisWorkload(tps, ack_log_path=ack_log_path)
    else:
        raise ValueError(f"Unknown engine: {engine}")
