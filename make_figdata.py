"""
Convert selected run logs into small pgfplots data files (2 s means) for the time-series figures.

Usage (repo root):  python make_figdata.py          (RESDIR env var selects the results directory)
Writes figdata/*.dat with columns:  t proxy knob target   (knob = J in ms for MongoDB, flush mode for MySQL)
Missing source runs are skipped; the manuscript includes each figure only if its data file exists.
"""
import json, os
from pathlib import Path

R = Path(os.environ.get("RESDIR", "results"))
OUT = Path("figdata")
SPECS = {
    "ts_mongo_pi":     "cl_mongo_pi_250tps_t0.3_r1.json",
    "ts_mongo_thr1":   "cl_mongo_threshold_250tps_T0.3_r1.json",
    "ts_mongo_thr250": "cl_mongo_threshold_250tps_V0.3_r1.json",
    "ts_mysql_pi":     "cl_mysql_pi_80tps_t0.3_r1.json",
    "ts_mongo_step":   "cl_mongo_pi_100tps_S0.3_r1.json",
    "ts_mysql_step":   "cl_mysql_pi_60tps_S0.3_r1.json",
}
BIN = 2.0


def main():
    OUT.mkdir(exist_ok=True)
    for name, fn in SPECS.items():
        p = R / fn
        if not p.exists():
            print(f"skip {name}: {fn} not found")
            continue
        d = json.load(open(p))
        tk, tgt = d["ticks"], d["rpo_star"]
        knob = "J_ms" if "J_ms" in tk[0] else "act"
        rows, cur, b0 = [], [], 0.0
        for x in tk:
            if x["t"] - b0 >= BIN and cur:
                rows.append((b0 + BIN / 2, sum(c["rpo_hat"] for c in cur) / len(cur), sum(c[knob] for c in cur) / len(cur)))
                cur, b0 = [], x["t"]
            cur.append(x)
        with open(OUT / f"{name}.dat", "w") as fh:
            fh.write("t proxy knob target\n")
            for t, px, kn in rows:
                fh.write(f"{t:.1f} {px:.5f} {kn:.2f} {tgt}\n")
        print(f"wrote figdata/{name}.dat ({len(rows)} rows)")


if __name__ == "__main__":
    main()
