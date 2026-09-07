"""
Bursty Workload Generator — Point 3.

Replaces the uniform token-bucket with a two-state Markov workload:
  - ON  state: TPS = base_tps × burst_factor  (burst)
  - OFF state: TPS = base_tps × 0.3           (quiet)

State transitions drawn from exponential distributions:
  - Mean ON  duration: t_on  = 30 s
  - Mean OFF duration: t_off = 20 s

This produces a realistic bursty write pattern that stresses
the PI controller's anti-windup mechanism.
"""
from __future__ import annotations
import logging
import random
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
from experiments.workload import _rand_payload, DOC_SIZE, BATCH_SIZE, REDIS_TTL

log = logging.getLogger(__name__)

T_ON_MEAN  = 30.0   # seconds
T_OFF_MEAN = 20.0   # seconds
QUIET_FRAC = 0.3    # quiet TPS = base × 0.3
BURST_MULT = 2.0    # burst TPS = base × 2.0


class BurstyTokenBucket:
    """Token bucket whose rate changes dynamically."""
    def __init__(self, rate: float):
        self.rate    = rate
        self.tokens  = rate
        self._last   = time.perf_counter()
        self._lock   = threading.Lock()

    def set_rate(self, rate: float):
        with self._lock:
            self.rate = max(rate, 0.1)

    def acquire(self):
        with self._lock:
            now     = time.perf_counter()
            elapsed = now - self._last
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            self._last  = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
        time.sleep(1.0 / max(self.rate, 0.1))


class BurstyMongoWorkload:
    def __init__(self, base_tps: float, seed: int = 0):
        self.base_tps = base_tps
        self.rng      = random.Random(seed)
        self._stop    = threading.Event()
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
        bucket = BurstyTokenBucket(self.base_tps / BATCH_SIZE)
        state  = "on"
        t_switch = time.time() + self.rng.expovariate(1.0 / T_ON_MEAN)
        log.info("Bursty MongoDB workload — base=%.0f TPS", self.base_tps)

        while not self._stop.is_set():
            now = time.time()
            if now >= t_switch:
                if state == "on":
                    state = "off"
                    rate  = (self.base_tps * QUIET_FRAC) / BATCH_SIZE
                    t_switch = now + self.rng.expovariate(1.0 / T_OFF_MEAN)
                else:
                    state = "on"
                    rate  = (self.base_tps * BURST_MULT) / BATCH_SIZE
                    t_switch = now + self.rng.expovariate(1.0 / T_ON_MEAN)
                bucket.set_rate(rate)
                log.debug("MongoDB workload → %s (%.0f TPS)", state,
                          rate * BATCH_SIZE)

            bucket.acquire()
            docs = [{"payload": _rand_payload(DOC_SIZE), "ts": time.time()}
                    for _ in range(BATCH_SIZE)]
            try:
                coll.insert_many(docs, ordered=False)
            except Exception as exc:
                log.debug("MongoDB bursty insert: %s", exc)

        client.close()


class BurstyMysqlWorkload:
    def __init__(self, base_tps: float, seed: int = 0):
        self.base_tps = base_tps
        self.rng      = random.Random(seed)
        self._stop    = threading.Event()
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
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS writes_bursty "
            "(id BIGINT AUTO_INCREMENT PRIMARY KEY, "
            " payload VARCHAR(256), ts DOUBLE)"
        )
        bucket = BurstyTokenBucket(self.base_tps)
        state  = "on"
        t_switch = time.time() + self.rng.expovariate(1.0 / T_ON_MEAN)
        log.info("Bursty MySQL workload — base=%.0f TPS", self.base_tps)

        while not self._stop.is_set():
            now = time.time()
            if now >= t_switch:
                if state == "on":
                    state = "off"
                    bucket.set_rate(self.base_tps * QUIET_FRAC)
                    t_switch = now + self.rng.expovariate(1.0 / T_OFF_MEAN)
                else:
                    state = "on"
                    bucket.set_rate(self.base_tps * BURST_MULT)
                    t_switch = now + self.rng.expovariate(1.0 / T_ON_MEAN)

            bucket.acquire()
            try:
                cursor.execute(
                    "INSERT INTO writes_bursty (payload, ts) VALUES (%s, %s)",
                    (_rand_payload(128), time.time()),
                )
            except Exception as exc:
                log.debug("MySQL bursty insert: %s", exc)

        cursor.close()
        conn.close()


class BurstyRedisWorkload:
    def __init__(self, base_tps: float, seed: int = 0):
        self.base_tps = base_tps
        self.rng      = random.Random(seed)
        self._stop    = threading.Event()
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
        bucket = BurstyTokenBucket(self.base_tps)
        state  = "on"
        t_switch = time.time() + self.rng.expovariate(1.0 / T_ON_MEAN)
        log.info("Bursty Redis workload — base=%.0f TPS", self.base_tps)

        while not self._stop.is_set():
            now = time.time()
            if now >= t_switch:
                if state == "on":
                    state = "off"
                    bucket.set_rate(self.base_tps * QUIET_FRAC)
                    t_switch = now + self.rng.expovariate(1.0 / T_OFF_MEAN)
                else:
                    state = "on"
                    bucket.set_rate(self.base_tps * BURST_MULT)
                    t_switch = now + self.rng.expovariate(1.0 / T_ON_MEAN)

            bucket.acquire()
            try:
                r.set(f"bk:{self._counter}", _rand_payload(64), ex=REDIS_TTL)
                self._counter += 1
            except Exception as exc:
                log.debug("Redis bursty SET: %s", exc)

        r.close()


def make_bursty_workload(engine: str, base_tps: float, seed: int = 0):
    if engine == "mongodb":
        return BurstyMongoWorkload(base_tps, seed)
    elif engine == "mysql":
        return BurstyMysqlWorkload(base_tps, seed)
    elif engine == "redis":
        return BurstyRedisWorkload(base_tps, seed)
    raise ValueError(f"Unknown engine: {engine}")
