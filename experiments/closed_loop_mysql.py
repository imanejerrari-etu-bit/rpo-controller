"""
Closed-loop test on MySQL PXC with a proxy that actually sees the actuator.

Proxy (a): unflushed redo gap on node 0, converted to seconds
    inst = (current_lsn - flushed_to_disk_lsn) / redo_write_rate
    rate = LSN growth over a sliding window (default 10 s)
    rpo_hat = EMA(inst, tau = 5 s)          # the raw gap is a 1 Hz sawtooth in mode 2

Actuator: innodb_flush_log_at_trx_commit in {1, 2}, applied on ALL nodes,
chosen from the PI output u by a 2-level hysteresis quantiser:
    mode 1 -> 2 when u > target*(1+h);   mode 2 -> 1 when u < target*(1-h)
The long-run mean of the proxy is regulated by integral action (mean error ~ 0
over a limit cycle); the instantaneous value is NOT regulated.

Policies:  pi | static1 | static2   (same proxy and logging, for a fair baseline)

Usage (repo root, port-forwards 33061-33063 active, MYSQL_PASS exported):
    python -m experiments.closed_loop_mysql --policy pi      --target 0.3 --tps 80 --duration 600
    python -m experiments.closed_loop_mysql --policy static1 --target 0.3 --tps 80 --duration 600
    python -m experiments.closed_loop_mysql --policy static2 --target 0.3 --tps 80 --duration 600
Output: results/cl_mysql_<policy>_<tps>tps.json
"""
from __future__ import annotations
import argparse, json, logging, math, statistics as st, time
from collections import deque
from pathlib import Path

from rpo_controller.config import EngineConfig, TS
from rpo_controller.pi_controller import PIController

log = logging.getLogger("closed_loop_mysql")


class GapProxy:
    """Redo-gap proxy. `read_lsn()` returns (current_lsn, flushed_lsn)."""

    def __init__(self, read_lsn, ts=TS, rate_window_s=10.0, ema_tau_s=5.0,
                 min_rate_Bps=2000.0):
        self.read_lsn, self.ts = read_lsn, ts
        self.win, self.min_rate = rate_window_s, min_rate_Bps
        self.alpha = 1.0 - math.exp(-ts / ema_tau_s)
        self.hist = deque()            # (t, current_lsn)
        self.ema = None
        self.last = {"gap_B": 0.0, "rate_Bps": 0.0, "inst": 0.0}

    def read(self, now):
        cur, fl = self.read_lsn()
        gap = max(0.0, cur - fl)
        self.hist.append((now, cur))
        while self.hist and now - self.hist[0][0] > self.win:
            self.hist.popleft()
        t0, l0 = self.hist[0]
        span = now - t0
        rate = (cur - l0) / span if span >= 2.0 else 0.0
        inst = gap / rate if rate >= self.min_rate else 0.0
        self.ema = inst if self.ema is None else self.ema + self.alpha * (inst - self.ema)
        self.last = {"gap_B": gap, "rate_Bps": rate, "inst": inst}
        return self.ema


class Hysteresis2:
    """Two-level actuator with hysteresis on the PI output u (seconds)."""

    def __init__(self, target, h=0.1, start_mode=2):
        self.up, self.down, self.mode = target * (1 + h), target * (1 - h), start_mode

    def __call__(self, u):
        if self.mode == 1 and u > self.up:
            self.mode = 2
        elif self.mode == 2 and u < self.down:
            self.mode = 1
        return self.mode


def summarise(ticks, target, ss_after, duration, acks):
    ss = [t for t in ticks if t["t"] >= ss_after]
    if not ss:
        return {}
    px = [t["rpo_hat"] for t in ss]
    modes = [t["act"] for t in ss]
    switches = sum(1 for a, b in zip(modes, modes[1:]) if a != b)
    return {
        "ss_mean_s": round(st.mean(px), 4), "ss_std_s": round(st.pstdev(px), 4),
        "mean_error_s": round(target - st.mean(px), 4),
        "frac_smoothed_above_target": round(sum(p > target for p in px) / len(px), 3),
        "frac_time_mode2": round(sum(m == 2 for m in modes) / len(modes), 3),
        "mode_switches": switches,
        "inst_gap_max_s": round(max(t["inst"] for t in ss), 3),
        "acked_inserts": acks, "throughput_ips": round(acks / duration, 1),
    }


def run_loop(proxy, policy, target, set_mode, duration, ss_after, kp, ki,
             clock=time.time, sleep=time.sleep, start_mode=2):
    """Pure control loop (testable without a database)."""
    cfg = EngineConfig(name="mysql", rpo_star=target, kp=kp, ki=ki)
    pi = PIController(cfg, dwb_min=0.001, dwb_max=35.0)
    quant = Hysteresis2(target, start_mode=start_mode)
    mode = {"pi": start_mode, "static1": 1, "static2": 2}[policy]
    quant.mode = mode
    set_mode(mode)
    ticks, t0 = [], clock()
    nxt = t0
    while clock() - t0 < duration:
        now = clock() - t0
        rpo_hat = proxy.read(now)
        dwb = pi.tick(rpo_hat)
        if policy == "pi":
            new = quant(dwb)
            if new != mode:
                set_mode(new)
                mode = new
        ticks.append({"t": round(now, 3), "rpo_hat": round(rpo_hat, 5), "dwb": round(dwb, 5),
                      "act": mode, "error": round(target - rpo_hat, 5),
                      "inst": round(proxy.last["inst"], 5),
                      "gap_B": proxy.last["gap_B"], "rate_Bps": round(proxy.last["rate_Bps"], 1)})
        nxt += TS
        sleep(max(0.0, nxt - clock()))
    return ticks


def main(policy, target, tps, duration, kp, ki, out_dir, tag=""):
    import mysql.connector
    from rpo_controller.config import MYSQL_USER, MYSQL_PASS, MYSQL_DB, mysql_pxc_pod_hosts
    from experiments.run_experiment import SETUP_FNS
    from experiments.workload import make_workload

    SETUP_FNS["mysql"]()
    conns = [mysql.connector.connect(host=h, port=p, user=MYSQL_USER, password=MYSQL_PASS,
                                     database=MYSQL_DB, autocommit=True, connection_timeout=10)
             for h, p in mysql_pxc_pod_hosts()]
    curs = [c.cursor() for c in conns]
    mismatches = {"n": 0}

    def read_lsn():
        curs[0].execute("SHOW GLOBAL STATUS WHERE Variable_name IN "
                        "('Innodb_redo_log_current_lsn','Innodb_redo_log_flushed_to_disk_lsn')")
        d = {k: float(v) for k, v in curs[0].fetchall()}
        return d["Innodb_redo_log_current_lsn"], d["Innodb_redo_log_flushed_to_disk_lsn"]

    def set_mode(m):
        for cur in curs:
            cur.execute(f"SET GLOBAL innodb_flush_log_at_trx_commit = {m}")
            cur.execute("SELECT @@GLOBAL.innodb_flush_log_at_trx_commit")
            if int(cur.fetchone()[0]) != m:
                mismatches["n"] += 1

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sfx = f"_{tag}" if tag else ""
    ack_path = out_dir / f"ack_mysql_{policy}_{int(tps)}tps{sfx}.csv"
    ack_path.unlink(missing_ok=True)      # fresh file every run (fixes cumulative count)
    wl = make_workload("mysql", tps, ack_log_path=str(ack_path))
    wl.start()
    time.sleep(10)
    try:
        ticks = run_loop(GapProxy(read_lsn), policy, target, set_mode, duration,
                         duration * 0.5, kp, ki)
    finally:
        wl.stop()
        set_mode(2)
        for c in conns:
            c.close()
    try:
        acks = sum(1 for _ in open(ack_path)) - 1       # minus header, approx
    except OSError:
        acks = 0
    summary = summarise(ticks, target, duration * 0.5, duration, max(acks, 0))
    summary["readback_mismatches"] = mismatches["n"]
    out = out_dir / f"cl_mysql_{policy}_{int(tps)}tps{sfx}.json"
    out.write_text(json.dumps({"engine": "mysql", "policy": policy, "rpo_star": target,
                               "tps": tps, "kp": kp, "ki": ki,
                               "workload_source": "token-bucket", "summary": summary,
                               "ticks": ticks}))
    print(json.dumps(summary, indent=2))
    print("saved", out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", choices=["pi", "static1", "static2"], required=True)
    ap.add_argument("--target", type=float, default=0.3)
    ap.add_argument("--tps", type=float, default=80)
    ap.add_argument("--duration", type=int, default=600)
    ap.add_argument("--kp", type=float, default=0.5)
    ap.add_argument("--ki", type=float, default=0.10)
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--tag", default="", help="suffix for output files, e.g. t0.3_r2")
    a = ap.parse_args()
    main(a.policy, a.target, a.tps, a.duration, a.kp, a.ki, a.out_dir, a.tag)
