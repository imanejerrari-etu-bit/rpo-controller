"""
MongoDB open-loop step response (NO controller).

Question: which WiredTiger journal counters, measured by the engine itself, react to
journalCommitInterval (J)?  If the sync rate follows 1/J (and the sync latency is
observable), an exposure proxy can be built that does NOT read the commanded value.

For each J (ordered so the sequence repeats), under a live workload, record every second
    log sync operations, log sync time duration (usecs), log flush operations, log bytes written
and report per segment (after discarding `settle` seconds):
    sync_per_s, flush_per_s, bytes_per_s, mean_sync_ms, interval_between_syncs_ms

Usage (repo root, venv active, port-forward 27017 active, MONGO_URI exported with credentials):
    python -m experiments.step_response_mongo --tps 250 --hold 120
Output: results/step_mongo_<tps>tps.json
"""
from __future__ import annotations
import argparse, json, logging, time
from pathlib import Path

log = logging.getLogger("step_response_mongo")
KEYS = {
    "sync_ops": "log sync operations",
    "sync_us": "log sync time duration (usecs)",
    "flush_ops": "log flush operations",
    "bytes": "log bytes written",
}


def read_counters(client):
    st = client["admin"].command("serverStatus")["wiredTiger"]["log"]
    return {k: float(st.get(v, 0.0)) for k, v in KEYS.items()}


def segment_stats(ticks, settle):
    """ticks: list of {'t': s, <counters>}.  Rates from first to last tick after `settle`."""
    t0 = ticks[0]["t"]
    use = [x for x in ticks if x["t"] - t0 >= settle]
    if len(use) < 3:
        return {}
    a, b = use[0], use[-1]
    dt = b["t"] - a["t"]
    dsync, dflush = b["sync_ops"] - a["sync_ops"], b["flush_ops"] - a["flush_ops"]
    out = {
        "sync_per_s": round(dsync / dt, 3),
        "flush_per_s": round(dflush / dt, 3),
        "bytes_per_s": round((b["bytes"] - a["bytes"]) / dt, 1),
        "mean_sync_ms": round((b["sync_us"] - a["sync_us"]) / dsync / 1000.0, 3) if dsync > 0 else None,
        "interval_between_syncs_ms": round(1000.0 * dt / dsync, 1) if dsync > 0 else None,
    }
    return out


def main(tps, hold, settle, levels, out_dir):
    from rpo_controller.config import MONGO_URI
    import pymongo
    from experiments.run_experiment import SETUP_FNS
    from experiments.workload import make_workload

    SETUP_FNS["mongodb"]()
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    wl = make_workload("mongodb", tps)
    wl.start()
    time.sleep(10)
    segments = []
    try:
        for idx, J in enumerate(levels):
            client["admin"].command({"setParameter": 1, "journalCommitInterval": int(J)})
            rb = client["admin"].command({"getParameter": 1, "journalCommitInterval": 1})["journalCommitInterval"]
            log.info("segment %d: J=%s ms, readback=%s", idx, J, rb)
            ticks, t_seg, nxt = [], time.time(), time.time()
            while time.time() - t_seg < hold:
                ticks.append({"t": round(time.time(), 3), **read_counters(client)})
                nxt += 1.0
                time.sleep(max(0.0, nxt - time.time()))
            segments.append({"idx": idx, "J_ms": J, "readback": rb, "stats": segment_stats(ticks, settle),
                             "ticks": ticks})
    finally:
        wl.stop()
        client["admin"].command({"setParameter": 1, "journalCommitInterval": 100})
        client.close()

    print("\nseg  J(ms) readback | sync/s  flush/s   bytes/s  mean_sync(ms)  interval_between_syncs(ms)")
    for s in segments:
        st = s["stats"]
        print(f"{s['idx']:>3} {s['J_ms']:>6} {s['readback']:>8} | {st.get('sync_per_s')!s:>6} "
              f"{st.get('flush_per_s')!s:>8} {st.get('bytes_per_s')!s:>9} {st.get('mean_sync_ms')!s:>14} "
              f"{st.get('interval_between_syncs_ms')!s:>26}")
    out = Path(out_dir) / f"step_mongo_{int(tps)}tps.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tps": tps, "hold_s": hold, "levels": levels, "segments": segments}))
    print("saved", out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--tps", type=float, default=250)
    ap.add_argument("--hold", type=int, default=120, help="seconds per segment")
    ap.add_argument("--settle", type=int, default=20)
    ap.add_argument("--levels", type=int, nargs="+", default=[500, 100, 20, 500, 100, 20],
                    help="journalCommitInterval values (ms), 1-500")
    ap.add_argument("--out-dir", default="results")
    a = ap.parse_args()
    main(a.tps, a.hold, a.settle, a.levels, a.out_dir)
