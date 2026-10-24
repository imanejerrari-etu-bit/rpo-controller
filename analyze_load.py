"""
Load-invariance table (closed loop, target 0.3 s) for MySQL PXC and MongoDB.

Combines the nominal-load runs already done (MySQL 80 TPS: cl_mysql_pi_80tps_t0.3_r*.json,
MongoDB 250 TPS: cl_mongo_pi_250tps_t0.3_r*.json) with the load runs
(cl_<engine>_pi_<tps>tps_L<tps>_r*.json).  Runs flagged in the sweep logs, and runs whose
tick series shows a stall (last tick < 570 s or gap > 5 s), are excluded and listed.

Usage (repo root):  python analyze_load.py [target]      (default 0.3)
Writes results/load_summary.csv
"""
import csv, glob, json, math, re, statistics as st, sys
from pathlib import Path

R = Path("results")
TARGET = float(sys.argv[1]) if len(sys.argv) > 1 else 0.3
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}


def ci(v):
    n, m = len(v), st.mean(v)
    return (m, float("nan")) if n < 2 else (m, T95.get(n - 1, 1.96) * st.stdev(v) / math.sqrt(n))


def load_flags():
    bad = {}
    for fn, key in (("sweep_log.csv", "mysql_main"), ("sweep_log_mongo.csv", "mongo_main"), ("sweep_log_load.csv", "load")):
        p = R / fn
        if p.exists():
            for r in csv.DictReader(open(p)):
                if r["flag"] != "ok":
                    bad[(key, r["tag"])] = r["flag"]
    return bad


def stats(path, engine):
    d = json.load(open(path))
    t = d["ticks"]
    if not t:
        return None, "no ticks"
    gaps = [b["t"] - a["t"] for a, b in zip(t, t[1:])]
    if t[-1]["t"] < 570 or (gaps and max(gaps) > 5):
        return None, f"stall (last tick {t[-1]['t']:.0f} s, max gap {max(gaps):.1f} s)"
    half = t[-1]["t"] * 0.5
    ss = [x for x in t if x["t"] >= half]
    s = d.get("summary", {}) or {}
    row = {"proxy": st.mean(x["rpo_hat"] for x in ss), "thr": s.get("throughput_ips", float("nan"))}
    if engine == "mysql":
        row["knob"] = 100.0 * sum(x["act"] == 2 for x in ss) / len(ss)          # % of time in mode 2
        row["chg"] = sum(1 for a, b in zip(ss, ss[1:]) if a["act"] != b["act"])
        row["fsync"] = float("nan")
    else:
        row["knob"] = st.mean(x["J_ms"] for x in ss)                              # mean J (ms)
        row["chg"] = sum(1 for a, b in zip(ss, ss[1:]) if a["J_ms"] != b["J_ms"])
        row["fsync"] = s.get("fsync_per_s", float("nan"))
    return row, None


def main():
    bad = load_flags()
    runs, excluded = {}, []
    specs = [("mysql", "mysql_main", f"cl_mysql_pi_80tps_t{TARGET}_r*.json", 80),
             ("mongo", "mongo_main", f"cl_mongo_pi_250tps_t{TARGET}_r*.json", 250),
             ("mysql", "load", "cl_mysql_pi_*tps_L*_r*.json", None),
             ("mongo", "load", "cl_mongo_pi_*tps_L*_r*.json", None)]
    for engine, kind, pat, tps_fixed in specs:
        for f in sorted(glob.glob(str(R / pat))):
            name = Path(f).name
            m = re.search(r"_(\d+)tps_", name)
            tps = tps_fixed or int(m.group(1))
            rep = re.search(r"_r(\d+)\.json$", name).group(1)
            tag = f"t{TARGET}_r{rep}" if kind != "load" else f"{engine}_L{tps}_r{rep}"
            if (kind, tag) in bad:
                excluded.append((name, bad[(kind, tag)])); continue
            row, why = stats(f, engine)
            if row is None:
                excluded.append((name, why)); continue
            runs.setdefault((engine, tps), []).append(row)

    hdr = ("engine", "offered_tps", "n", "proxy_mean", "proxy_ci95", "err_%_of_target",
           "mode2_%|J_ms", "changes_in_300s", "fsync_per_s", "acked_ips", "acked_ci95")
    print("\n" + " | ".join(f"{h:>14}" for h in hdr))
    rows = []
    for (eng, tps) in sorted(runs):
        g = runs[(eng, tps)]
        pm, pc = ci([x["proxy"] for x in g]); tm, tc = ci([x["thr"] for x in g])
        row = (eng, tps, len(g), round(pm, 4), round(pc, 4), round(100 * (pm - TARGET) / TARGET, 2),
               round(st.mean(x["knob"] for x in g), 1), round(st.mean(x["chg"] for x in g), 0),
               round(st.mean(x["fsync"] for x in g), 2) if eng == "mongo" else "-", round(tm, 1), round(tc, 1))
        rows.append(row)
        print(" | ".join(f"{v:>14}" for v in row))
        if len(g) < 3:
            print(f"   WARNING: only n={len(g)} valid run(s) for {eng} at {tps} TPS: CI unreliable")
    if excluded:
        print("\nExcluded runs (report them in the paper or repeat them):")
        for n, w in excluded:
            print(f"  {n}: {w}")
    print("\n'acked_ips' = measured acknowledged inserts/s (the generator's real rate differs from the nominal TPS).")
    with open(R / "load_summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(hdr); w.writerows(rows)
    print("saved results/load_summary.csv  (CI = Student t, n small)")


if __name__ == "__main__":
    main()
