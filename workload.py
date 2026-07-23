"""
Token-Bucket Workload Generator — Section V-A of the paper.

MongoDB : insertMany(10 × 128 B documents)
MySQL   : INSERT into test table
Redis   : SET key value (TTL = 300 s)

Each generator runs in its own thread at a specified TPS.
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
    """Simple token-bucket rate limiter."""

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


# ─────────────────────────────────────────────────────────────────────────────
# Per-engine generators
# ─────────────────────────────────────────────────────────────────────────────

class MongoWorkload:
    """insertMany at BATCH_SIZE × DOC_SIZE bytes per call."""

    def __init__(self, tps: float):
        self.tps    = tps
        self._stop  = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        client = pymongo.MongoClient(MONGO_URI)
        coll   = client["workload"]["writes"]
        bucket = TokenBucket(self.tps / BATCH_SIZE)   # batches per second
        log.info("MongoDB workload started — %.0f TPS", self.tps)

        while not self._stop.is_set():
            bucket.acquire()
            docs = [{"payload": _rand_payload(DOC_SIZE), "ts": time.time()}
                    for _ in range(BATCH_SIZE)]
            try:
                coll.insert_many(docs, ordered=False)
            except Exception as exc:
                log.debug("MongoDB insert error: %s", exc)

        client.close()
        log.info("MongoDB workload stopped")


class MysqlWorkload:
    """Simple INSERT at given TPS."""

    def __init__(self, tps: float):
        self.tps   = tps
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
        bucket = TokenBucket(self.tps)
        log.info("MySQL workload started — %.0f TPS", self.tps)

        while not self._stop.is_set():
            bucket.acquire()
            payload = _rand_payload(128)
            try:
                cursor.execute(
                    "INSERT INTO writes (payload, ts) VALUES (%s, %s)",
                    (payload, time.time()),
                )
            except Exception as exc:
                log.debug("MySQL insert error: %s", exc)

        cursor.close()
        conn.close()
        log.info("MySQL workload stopped")


class RedisWorkload:
    """SET key value with TTL=300 s at given TPS."""

    def __init__(self, tps: float):
        self.tps   = tps
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

    def _run(self):
        r      = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT,
                                  password=REDIS_PASS or None,
                                  decode_responses=True)
        bucket = TokenBucket(self.tps)
        log.info("Redis workload started — %.0f TPS", self.tps)

        while not self._stop.is_set():
            bucket.acquire()
            key     = f"k:{self._counter}"
            value   = _rand_payload(64)
            self._counter += 1
            try:
                r.set(key, value, ex=REDIS_TTL)
            except Exception as exc:
                log.debug("Redis SET error: %s", exc)

        r.close()
        log.info("Redis workload stopped")


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_workload(engine: str, tps: float):
    if engine == "mongodb":
        return MongoWorkload(tps)
    elif engine == "mysql":
        return MysqlWorkload(tps)
    elif engine == "redis":
        return RedisWorkload(tps)
    else:
        raise ValueError(f"Unknown engine: {engine}")
