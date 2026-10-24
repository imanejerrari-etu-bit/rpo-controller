"""
Aggregate the MongoDB closed-loop sweep (results/cl_mongo_pi_<tps>tps_<PFX><target>_r<rep>.json).

Excludes runs flagged in results/sweep_log_mongo.csv (flag != ok). Per variant (PFX: 't' = PI,
'P' = P-only, 'T' = threshold J in {1,500}, 'U' = {150,450}, 'V' = {250,350}) and per target reports mean +- 95 % Student-t CI over repetitions of:
  proxy mean (steady state), error vs target, J mean, fsync/s, throughput, J changes.
Also reports the fsync-rate ratio between the smallest and the largest target (cost of tight SLAs).

Usage (repo root):  python analyze_sweep_mongo.py [tps]      (default 250)
Writes results/sweep_mongo_summary.csv
"""
import csv, glob, json, math, re, statistics as st, sys
from pathlib import Path

R = Path("results")
TPS = int(sys.argv[1]) if len(sys.argv) > 1 else 250
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}


def ci(v):
    n, m = len(v), st.mean(v)
    if n < 2:
        return m, float("nan")
    return m, T95.get(n - 1, 1.96) * st.stdev(v) / math.sqrt(n)


def flagged():
    bad = set()
    p = R / "sweep_log_mongo.csv"
    if p.exists():
        for row in csv.DictReader(open(p)):
            if row["flag"] != "ok":
                bad.add(row["tag"])
    return bad


def main():
    bad = flagged()
    groups = {}
    for f in sorted(glob.glob(str(R / f"cl_mongo_*_{TPS}tps_*_r*.json"))):
        m = re.search(r"_([A-Za-z]+)([0-9.]+)_r(\d+)\.json$", f)
        if not m:
            continue
        pfx, T, rep = m.group(1), float(m.group(2)), m.group(3)
        tag = f"{pfx}{m.group(2)}_r{rep}"
        if tag in bad:
            print(f"excluded (flagged): {tag}")
            continue
        s = json.load(open(f))["summary"]
        if not s:
            print(f"excluded (empty summary): {tag}")
            continue
        groups.setdefault((pfx, T), []).append(s)

    hdr = ("variant", "target", "n", "proxy_mean", "proxy_ci95", "err_vs_target", "J_mean_ms",
           "fsync_per_s", "fsync_ci95", "thr_ips", "thr_ci95", "J_changes", "mismatch")
    print("\n" + " | ".join(f"{h:>13}" for h in hdr))
    rows, fsync_by = [], {}
    for (pfx, T) in sorted(groups):
        g = groups[(pfx, T)]
        pm, pc = ci([x["ss_mean_s"] for x in g])
        fm, fc = ci([x["fsync_per_s"] for x in g])
        tm, tc = ci([x["throughput_ips"] for x in g])
        row = ("PI" if pfx == "t" else ("P-only" if pfx == "P" else ({"T": "thr 1/500", "U": "thr 150/450", "V": "thr 250/350"}.get(pfx, pfx))), T, len(g), round(pm, 4), round(pc, 4),
               round(T - pm, 4), round(st.mean(x["J_mean_ms"] for x in g), 1), round(fm, 2), round(fc, 2),
               round(tm, 1), round(tc, 1), round(st.mean(x["J_changes"] for x in g), 0),
               sum(x.get("readback_mismatches", 0) for x in g))
        rows.append(row)
        fsync_by[(row[0], T)] = fm
        print(" | ".join(f"{v:>13}" for v in row))
    pi = {T: v for (var, T), v in fsync_by.items() if var == "PI"}
    if len(pi) >= 2:
        lo, hi = min(pi), max(pi)
        print(f"\nPI: fsync/s at target {lo} s = {pi[lo]:.2f}  vs  target {hi} s = {pi[hi]:.2f}  (x{pi[lo] / pi[hi]:.1f})")
    with open(R / "sweep_mongo_summary.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(hdr); w.writerows(rows)
    print("\nsaved results/sweep_mongo_summary.csv  (CI = Student t, n small: read with care)")


if __name__ == "__main__":
    main()
