"""
Aggregate the MySQL closed-loop sweep.

Reads results/cl_mysql_pi_80tps_t<target>_r<rep>.json, excludes runs flagged in
results/sweep_log.csv (flag != ok), and reports per target:
  - steady-state proxy mean (t >= duration/2), mean over repetitions, std, 95 % Student-t CI
  - throughput (acked inserts/s), same statistics
  - fraction of time in mode 2, number of mode switches, worst instantaneous gap
and the throughput gain relative to the static baselines if their files exist.

Usage (repo root):  python analyze_sweep.py
Writes results/sweep_summary.csv
"""
import csv, glob, json, math, re, statistics as st
from pathlib import Path

R = Path("results")
# two-sided 95 % Student-t critical values, df = n-1
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}


def ci(vals):
    n = len(vals)
    m = st.mean(vals)
    if n < 2:
        return m, float("nan"), float("nan")
    s = st.stdev(vals)
    h = T95.get(n - 1, 1.96) * s / math.sqrt(n)
    return m, s, h


def flagged_tags():
    bad = set()
    p = R / "sweep_log.csv"
    if p.exists():
        for row in csv.DictReader(open(p)):
            if row["flag"] != "ok":
                bad.add(row["tag"])
    return bad


def load_run(path):
    d = json.load(open(path))
    s = d["summary"]
    dur = d["ticks"][-1]["t"]
    ss = [t["rpo_hat"] for t in d["ticks"] if t["t"] >= dur * 0.5]
    return {"ss_mean": st.mean(ss), "thr": s["throughput_ips"],
            "duty2": s["frac_time_mode2"], "sw": s["mode_switches"],
            "inst_max": s["inst_gap_max_s"], "mism": s.get("readback_mismatches", 0)}


def main():
    bad = flagged_tags()
    runs = {}
    for f in sorted(glob.glob(str(R / "cl_mysql_pi_80tps_t*_r*.json"))):
        m = re.search(r"_t([0-9.]+)_r(\d+)\.json$", f)
        if not m:
            continue
        tag = f"t{m.group(1)}_r{m.group(2)}"
        if tag in bad:
            print(f"excluded (flagged): {tag}")
            continue
        runs.setdefault(float(m.group(1)), []).append(load_run(f))

    base, warn = {}, []
    for name in ("static1", "static2"):
        same = []
        for f in sorted(glob.glob(str(R / f"cl_mysql_{name}_80tps_r*.json"))):
            tag = f"{name}_" + re.search(r"_(r\d+)\.json$", f).group(1)
            if tag in bad:
                print(f"excluded (flagged): {tag}")
                continue
            same.append(load_run(f))
        if same:
            base[name] = {k: (max if k == "inst_max" else st.mean)([x[k] for x in same]) for k in same[0]}
            base[name]["n"] = len(same)
        elif (R / f"cl_mysql_{name}_80tps.json").exists():
            base[name] = load_run(R / f"cl_mysql_{name}_80tps.json")
            base[name]["n"] = 1
            warn.append(name)

    rows = []
    hdr = ("target", "n", "proxy_mean", "proxy_ci95", "err_vs_target", "thr_mean", "thr_ci95",
           "duty_mode2", "switches", "inst_gap_max", "thr_gain_vs_static1_%")
    print("\n" + " | ".join(f"{h:>13}" for h in hdr))
    for T in sorted(runs):
        r = runs[T]
        pm, ps, ph = ci([x["ss_mean"] for x in r])
        tm, ts, th = ci([x["thr"] for x in r])
        gain = (100 * (tm / base["static1"]["thr"] - 1)) if "static1" in base else float("nan")
        row = (T, len(r), round(pm, 4), round(ph, 4), round(T - pm, 4), round(tm, 1), round(th, 1),
               round(st.mean(x["duty2"] for x in r), 3), round(st.mean(x["sw"] for x in r), 1),
               round(max(x["inst_max"] for x in r), 2), round(gain, 1))
        rows.append(row)
        print(" | ".join(f"{v:>13}" for v in row))
    for k in warn:
        print(f"\nWARNING: baseline {k} comes from ANOTHER session (single run): the gain column is NOT reliable")
    for k, v in base.items():
        print(f"\nbaseline {k} (n={v['n']}): proxy {v['ss_mean']:.4f} s, throughput {v['thr']} ips, "
              f"mode-2 duty {v['duty2']}, worst gap {v['inst_max']} s")
    if any(x["mism"] for r in runs.values() for x in r):
        print("\nWARNING: some runs have readback mismatches")
    with open(R / "sweep_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows)
    print("\nsaved results/sweep_summary.csv  (CI = Student t, n small: read with care)")


if __name__ == "__main__":
    main()
