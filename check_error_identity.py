"""
Error-accounting identity for the PI controller with dead band (exact, plant-independent).

Controller (pi_controller.py): e_k = target - proxy_k ; sigma integrates e_k*Ts ONLY when |e_k| > eps_d
(eps_d = 0.05*target), clamped to +-I_MAX.   Over a steady-state window of N ticks (T = N*Ts):

    sum_k e_k  =  sum_{active} e_k  +  sum_{inactive} e_k
    sum_{active} e_k * Ts = sigma_end - sigma_start      (if sigma never touches +-I_MAX)
    => |mean error| <= |sigma_end - sigma_start| / T  +  eps_d * (N_inactive / N)

So the mean tracking error is bounded by the drift of the integrator state plus the dead band; it does
not depend on the plant or on the granularity of the actuator.  What the plant decides is whether
sigma stays bounded (target reachable) or runs into the clamp (target unreachable).

This script rebuilds sigma from the logged tick errors, checks the reconstruction against the logged
controller output, and reports the terms of the bound for each PI run.

Usage (repo root):  python check_error_identity.py        # writes results/error_identity.csv
"""
import csv, glob, json, statistics as st
from pathlib import Path

R = Path("results")
TS, I_MAX = 0.5, 10.0


def analyse(path):
    d = json.load(open(path))
    t, kp, ki, target = d["ticks"], d.get("kp"), d.get("ki"), d["rpo_star"]
    if kp is None or ki is None or not t:
        return None
    eps = 0.05 * target
    sigma, sig, recon_err, clamped = 0.0, [], 0.0, False
    for x in t:                                     # rebuild sigma exactly as the controller does
        e = x["error"]
        if abs(e) > eps:
            raw = sigma + e * TS
            if abs(raw) > I_MAX:
                clamped = True
            sigma = max(-I_MAX, min(I_MAX, raw))
        sig.append(sigma)
        lo, hi = (0.001, 0.5) if "J_ms" in x else (0.001, 35.0)
        u = max(lo, min(hi, kp * e + ki * sigma))
        recon_err = max(recon_err, abs(u - x["dwb"]))
    half = t[-1]["t"] * 0.5
    idx = [i for i, x in enumerate(t) if x["t"] >= half]
    N, T = len(idx), len(idx) * TS
    e = [t[i]["error"] for i in idx]
    active = [abs(v) > eps for v in e]
    mean_e = st.mean(e)
    dsig = sig[idx[-1]] - sig[idx[0] - 1]
    n_in = sum(not a for a in active)
    bound = abs(dsig) / T + eps * n_in / N
    return {"mean_error": mean_e, "drift_term": dsig / T, "deadband_term": eps * n_in / N, "bound": bound,
            "frac_inactive": n_in / N, "sigma_max_abs": max(abs(v) for v in sig), "clamped": clamped,
            "recon_err": recon_err, "holds": abs(mean_e) <= bound + 1e-9}


def main():
    rows = []
    files = sorted(glob.glob(str(R / "cl_mongo_pi_*tps_[tP]*_r*.json")) + glob.glob(str(R / "cl_mysql_pi_*tps_[tL]*_r*.json")))
    hdr = ("run", "mean_error_s", "drift_term", "deadband_term", "bound_s", "frac_in_band", "sigma_max", "clamped", "recon_err", "holds")
    print(" | ".join(f"{h:>13}" for h in hdr[:1]) + " | " + " | ".join(f"{h:>12}" for h in hdr[1:]))
    for f in files:
        r = analyse(f)
        if r is None:
            continue
        row = (Path(f).name.replace("cl_", "").replace(".json", ""), round(r["mean_error"], 5), round(r["drift_term"], 5),
               round(r["deadband_term"], 5), round(r["bound"], 5), round(r["frac_inactive"], 3),
               round(r["sigma_max_abs"], 2), r["clamped"], f"{r['recon_err']:.1e}", r["holds"])
        rows.append(row)
        print(f"{row[0]:>40} | " + " | ".join(f"{v!s:>12}" for v in row[1:]))
    bad = [r for r in rows if not r[-1]]
    print(f"\n{len(rows)} runs ; bound violated: {len(bad)} ; sigma clamped in: {sum(1 for r in rows if r[7])} ; "
          f"max reconstruction error of the controller output: {max(float(r[8]) for r in rows):.1e} s")
    with open(R / "error_identity.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(hdr); w.writerows(rows)
    print("saved results/error_identity.csv")


if __name__ == "__main__":
    main()
