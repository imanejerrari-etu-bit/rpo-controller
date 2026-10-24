"""
MongoDB closed-loop test with an engine-measured proxy.

Proxy: realised journal flush period (seconds) over a sliding window W (default 10 s):
    rpo_hat = (t_now - t_old) / (log_flush_ops_now - log_flush_ops_old)
Measured: in step experiments cycle ~= J + p*L_sync (p = syncs/flushes), within 1 % (12 points).
It does NOT read the commanded journalCommitInterval, so a change in storage latency or
load shows up in the proxy.

Actuator: journalCommitInterval J = clamp(round(1000 * dwb), 1, 500) ms, continuous PI output.

Policies:  pi | static | threshold  (static: --static-j MS fixed J; threshold: bang-bang J in {1, 500} ms
           with a +-5 % hysteresis band around the target; same proxy and logging -> baselines)
Cost metrics: fsync/s (log sync operations), flush/s, acked inserts/s (workload ack log).

Usage (repo root, venv active, port-forward 27017 active, MONGO_URI exported with credentials):
    python -m experiments.closed_loop_mongo --policy pi --target 0.3 --tps 250 --duration 600 --tag t0.3_r1
    python -m experiments.closed_loop_mongo --policy static --static-j 295 --target 0.3 --tps 250 --duration 600 --tag s295_r1
Output: results/cl_mongo_<policy>_<tps>tps_<tag>.json
"""
from __future__ import annotations
import argparse, json, logging, statistics as st, time
from collections import deque
from pathlib import Path

from rpo_controller.config import EngineConfig, TS
from rpo_controller.pi_controller import PIController

log = logging.getLogger("closed_loop_mongo")


class FlushCycleProxy:
    """`read_counters()` -> dict(flush_ops, sync_ops, sync_us, bytes) (cumulative)."""

    def __init__(self, read_counters, window_s=10.0, initial=0.1):
        self.read, self.win = read_counters, window_s
        self.hist = deque()
        self.last = initial
        self.c = {}

    def read_proxy(self, now):
        self.c = self.read()
        self.hist.append((now, self.c["flush_ops"]))
        while len(self.hist) > 1 and now - self.hist[1][0] >= self.win:
            self.hist.popleft()
        t0, f0 = self.hist[0]
        df = self.c["flush_ops"] - f0
        if now - t0 >= 2.0 and df > 0:
            self.last = (now - t0) / df
        return self.last


def run_loop(proxy, policy, target, set_j, duration, kp, ki, static_j_ms=None, j0_ms=100,
             thr_low_ms=1, thr_high_ms=500,
             clock=time.time, sleep=time.sleep):
    cfg = EngineConfig(name="mongodb", rpo_star=target, kp=kp, ki=ki)
    pi = PIController(cfg, dwb_min=0.001, dwb_max=0.5)
    J = int(static_j_ms if policy == "static" else j0_ms)
    set_j(J)
    hyst = 0.05            # threshold policy: +-5 % band around the target (same width as the PI dead band)
    ticks, t0 = [], clock()
    nxt = t0
    while clock() - t0 < duration:
        now = clock() - t0
        rpo_hat = proxy.read_proxy(now)
        dwb = pi.tick(rpo_hat)
        if policy == "pi":
            newJ = max(1, min(500, int(round(1000 * dwb))))
            if newJ != J:
                set_j(newJ)
                J = newJ
        elif policy == "threshold":      # bang-bang with hysteresis: J_min above the band, J_max below it
            newJ = J
            if rpo_hat > target * (1 + hyst):
                newJ = int(thr_low_ms)
            elif rpo_hat < target * (1 - hyst):
                newJ = int(thr_high_ms)
            if newJ != J:
                set_j(newJ)
                J = newJ
        ticks.append({"t": round(now, 3), "rpo_hat": round(rpo_hat, 5), "dwb": round(dwb, 5), "J_ms": J,
                      "error": round(target - rpo_hat, 5), **{k: proxy.c[k] for k in proxy.c}})
        nxt += TS
        sleep(max(0.0, nxt - clock()))
    return ticks


def summarise(ticks, target, duration, acks, thr_duration=None):
    ss = [t for t in ticks if t["t"] >= duration * 0.5]
    if len(ss) < 3:
        return {}
    px = [t["rpo_hat"] for t in ss]
    a, b = ss[0], ss[-1]
    dt = b["t"] - a["t"]
    gaps = [y["t"] - x["t"] for x, y in zip(ticks, ticks[1:])]
    max_gap = max(gaps) if gaps else 0.0
    return {
        "last_tick_s": round(ticks[-1]["t"], 1), "max_tick_gap_s": round(max_gap, 2),
        "valid": bool(ticks[-1]["t"] >= 0.95 * duration and max_gap <= 5.0),
        "ss_mean_s": round(st.mean(px), 4), "ss_std_s": round(st.pstdev(px), 4),
        "mean_error_s": round(target - st.mean(px), 4),
        "frac_above_target": round(sum(p > target for p in px) / len(px), 3),
        "J_mean_ms": round(st.mean(t["J_ms"] for t in ss), 1),
        "J_changes": sum(1 for x, y in zip(ss, ss[1:]) if x["J_ms"] != y["J_ms"]),
        "fsync_per_s": round((b["sync_ops"] - a["sync_ops"]) / dt, 2),
        "flush_per_s": round((b["flush_ops"] - a["flush_ops"]) / dt, 2),
        "mean_sync_ms": round((b["sync_us"] - a["sync_us"]) / max(b["sync_ops"] - a["sync_ops"], 1) / 1000, 3),
        "acked_inserts": acks, "throughput_ips": round(acks / (thr_duration or duration), 1),
    }


def main(policy, target, tps, duration, kp, ki, static_j, out_dir, tag, window, j0=100, thr_low=1, thr_high=500):
    import pymongo
    from rpo_controller.config import MONGO_URI
    from experiments.run_experiment import SETUP_FNS
    from experiments.workload import make_workload

    SETUP_FNS["mongodb"]()
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    keys = {"flush_ops": "log flush operations", "sync_ops": "log sync operations",
            "sync_us": "log sync time duration (usecs)", "bytes": "log bytes written"}

    def read_counters():
        s = client["admin"].command("serverStatus")["wiredTiger"]["log"]
        return {k: float(s.get(v, 0.0)) for k, v in keys.items()}

    mism = {"n": 0}

    def set_j(J):
        client["admin"].command({"setParameter": 1, "journalCommitInterval": int(J)})
        if int(client["admin"].command({"getParameter": 1, "journalCommitInterval": 1})["journalCommitInterval"]) != int(J):
            mism["n"] += 1

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sfx = f"_{tag}" if tag else ""
    ack = out_dir / f"ack_mongo_{policy}_{int(tps)}tps{sfx}.csv"
    ack.unlink(missing_ok=True)
    wl = make_workload("mongodb", tps, ack_log_path=str(ack))
    wl.start()
    time.sleep(10)
    t_loop0 = time.time()
    try:
        ticks = run_loop(FlushCycleProxy(read_counters, window), policy, target, set_j, duration,
                         kp, ki, static_j_ms=static_j, j0_ms=j0,
                         thr_low_ms=thr_low, thr_high_ms=thr_high)
        t_loop1 = time.time()
    finally:
        wl.stop()
        set_j(100)
        client.close()
    acks = 0
    try:
        with open(ack) as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) == 2 and t_loop0 <= float(parts[1]) <= t_loop1:
                    acks += 1
    except (OSError, ValueError, NameError):
        acks = 0
    dur_eff = (t_loop1 - t_loop0) if "t_loop1" in dir() else duration
    summary = summarise(ticks, target, duration, acks, thr_duration=dur_eff)
    summary["readback_mismatches"] = mism["n"]
    out = out_dir / f"cl_mongo_{policy}_{int(tps)}tps{sfx}.json"
    out.write_text(json.dumps({"engine": "mongodb", "policy": policy, "rpo_star": target, "tps": tps,
                               "kp": kp, "ki": ki, "static_j_ms": static_j, "window_s": window,
                               "summary": summary, "ticks": ticks}))
    print(json.dumps(summary, indent=2))
    print("saved", out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", choices=["pi", "static", "threshold"], required=True)
    ap.add_argument("--target", type=float, default=0.3)
    ap.add_argument("--tps", type=float, default=250)
    ap.add_argument("--duration", type=int, default=600)
    ap.add_argument("--kp", type=float, default=0.8)
    ap.add_argument("--ki", type=float, default=0.20)
    ap.add_argument("--static-j", type=int, default=None, help="fixed J in ms (policy static)")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--j0", type=int, default=100, help="initial J (ms) for policy pi")
    ap.add_argument("--thr-low", type=int, default=1, help="threshold policy: J (ms) when the proxy is above the band")
    ap.add_argument("--thr-high", type=int, default=500, help="threshold policy: J (ms) when the proxy is below the band")
    ap.add_argument("--tag", default="")
    ap.add_argument("--out-dir", default="results")
    a = ap.parse_args()
    if a.policy == "static" and a.static_j is None:
        ap.error("--static-j is required with --policy static")
    main(a.policy, a.target, a.tps, a.duration, a.kp, a.ki, a.static_j, a.out_dir, a.tag, a.window, a.j0, a.thr_low, a.thr_high)
