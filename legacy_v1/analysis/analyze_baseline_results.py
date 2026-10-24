"""
analyze_baseline_results.py
=============================

Computes the SAME four metrics used throughout the paper (Section 4.4,
"Metrics") from per-tick run logs, for the main controller and both
baselines, so the numbers are directly comparable and can replace the
qualitative Table 4 with real figures.

Expected input format
----------------------
One CSV per (engine, controller_variant, run_id), OR one combined CSV
with columns:

    engine, controller, run_id, t, rpo_star, rpo_hat

- `controller` in {"main", "naive_pid", "arima_ff"}
- `t` in seconds from run start (warmup/cooldown rows can be included;
  they are trimmed automatically per the paper's protocol: warmup=10s,
  cooldown=15s, steady-state window t>300s out of a 600s run -- adjust
  WARMUP_S / COOLDOWN_S / STEADY_STATE_T below if your runs use
  different durations).

Usage
-----
    python analyze_baseline_results.py results.csv --out table4_rows.tex

This prints a summary table to stdout AND writes LaTeX tabular rows
you can paste directly into Table 4 (Section 5.8) of
rpo_controller_computing.tex, replacing the qualitative comparison
with measured numbers.
"""

from __future__ import annotations
import argparse
import csv
import math
import random
from collections import defaultdict
from typing import Dict, List, Tuple

WARMUP_S = 10.0
COOLDOWN_S = 15.0
STEADY_STATE_T = 300.0     # matches paper: "steady-state ... for t > 300s"
RUN_DURATION_S = 600.0
CONV_TOL_FRAC = 0.10       # |rpo_hat - rpo_star| <= 0.10 * rpo_star
N_BOOTSTRAP = 2000
CI_LEVEL = 0.95


# ---------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------

def load_runs(path: str) -> Dict[Tuple[str, str], List[List[dict]]]:
    """Returns {(engine, controller): [run1_ticks, run2_ticks, ...]}
    where each run's ticks are dicts sorted by t."""
    raw: Dict[Tuple[str, str, str], List[dict]] = defaultdict(list)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (row["engine"], row["controller"], row["run_id"])
            raw[key].append({
                "t": float(row["t"]),
                "rpo_star": float(row["rpo_star"]),
                "rpo_hat": float(row["rpo_hat"]),
            })

    grouped: Dict[Tuple[str, str], List[List[dict]]] = defaultdict(list)
    for (engine, controller, _run_id), ticks in raw.items():
        ticks.sort(key=lambda r: r["t"])
        grouped[(engine, controller)].append(ticks)
    return grouped


# ---------------------------------------------------------------------
# Per-run metrics (definitions match Section 4.4 of the paper exactly)
# ---------------------------------------------------------------------

def trim_warmup_cooldown(ticks: List[dict]) -> List[dict]:
    t_end = ticks[-1]["t"] if ticks else 0.0
    return [r for r in ticks
            if WARMUP_S <= r["t"] <= (t_end - COOLDOWN_S)]


def steady_state_mean_std(ticks: List[dict]) -> Tuple[float, float]:
    vals = [r["rpo_hat"] for r in ticks if r["t"] > STEADY_STATE_T]
    if not vals:
        return float("nan"), float("nan")
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / max(1, len(vals) - 1)
    return mu, math.sqrt(var)


def violation_rate(ticks: List[dict]) -> float:
    if not ticks:
        return float("nan")
    n_violations = sum(1 for r in ticks if r["rpo_hat"] > r["rpo_star"])
    return n_violations / len(ticks)


def convergence_time(ticks: List[dict]) -> float:
    """First t (after warmup) at which |rpo_hat - rpo_star| <= tol."""
    for r in ticks:
        tol = CONV_TOL_FRAC * r["rpo_star"]
        if abs(r["rpo_hat"] - r["rpo_star"]) <= tol:
            return r["t"] - WARMUP_S
    return float("nan")  # never converged within the run


# ---------------------------------------------------------------------
# Across-run statistics
# ---------------------------------------------------------------------

def cv_percent(values: List[float]) -> float:
    vals = [v for v in values if not math.isnan(v)]
    if len(vals) < 2:
        return float("nan")
    mu = sum(vals) / len(vals)
    var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
    sd = math.sqrt(var)
    return 100.0 * sd / mu if mu != 0 else float("nan")


def bootstrap_ci(values: List[float], n_boot: int = N_BOOTSTRAP,
                  ci: float = CI_LEVEL, seed: int = 42) -> Tuple[float, float, float]:
    """Percentile bootstrap CI on the mean, matching the paper's
    '95% bootstrap confidence intervals (2000 resamples)' (Sec 4.4)."""
    rng = random.Random(seed)
    vals = [v for v in values if not math.isnan(v)]
    if not vals:
        return float("nan"), float("nan"), float("nan")
    n = len(vals)
    means = []
    for _ in range(n_boot):
        sample = [vals[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo_idx = int((1 - ci) / 2 * n_boot)
    hi_idx = int((1 + ci) / 2 * n_boot) - 1
    point = sum(vals) / n
    return point, means[lo_idx], means[hi_idx]


def cohens_d(a: List[float], b: List[float]) -> float:
    """Cohen's d effect size, a - b, pooled SD -- same statistic used
    elsewhere in your papers (e.g. the ComSIS HABench audit)."""
    a = [v for v in a if not math.isnan(v)]
    b = [v for v in b if not math.isnan(v)]
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((v - ma) ** 2 for v in a) / (len(a) - 1)
    vb = sum((v - mb) ** 2 for v in b) / (len(b) - 1)
    n_a, n_b = len(a), len(b)
    pooled_sd = math.sqrt(((n_a - 1) * va + (n_b - 1) * vb) / (n_a + n_b - 2))
    if pooled_sd == 0:
        return float("nan")
    return (ma - mb) / pooled_sd


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------

def summarize(grouped: Dict[Tuple[str, str], List[List[dict]]]) -> Dict:
    summary = {}
    per_run_violation: Dict[Tuple[str, str], List[float]] = {}

    for (engine, controller), runs in grouped.items():
        trimmed_runs = [trim_warmup_cooldown(r) for r in runs]
        viol_rates = [violation_rate(r) for r in trimmed_runs]
        conv_times = [convergence_time(r) for r in trimmed_runs]
        ss_means, ss_stds = zip(*[steady_state_mean_std(r) for r in trimmed_runs]) \
            if trimmed_runs else ([], [])

        point, lo, hi = bootstrap_ci(viol_rates)
        per_run_violation[(engine, controller)] = viol_rates

        summary[(engine, controller)] = {
            "n_runs": len(runs),
            "violation_rate": point,
            "violation_ci": (lo, hi),
            "cv_percent": cv_percent(list(ss_means)),
            "mean_conv_time_s": (
                sum(c for c in conv_times if not math.isnan(c)) /
                max(1, sum(1 for c in conv_times if not math.isnan(c)))
                if any(not math.isnan(c) for c in conv_times) else float("nan")
            ),
            "n_never_converged": sum(1 for c in conv_times if math.isnan(c)),
        }

    # Effect size of each baseline vs. "main", per engine
    engines = {e for (e, _c) in grouped.keys()}
    for engine in engines:
        main_key = (engine, "main")
        if main_key not in per_run_violation:
            continue
        for controller in ("naive_pid", "arima_ff"):
            k = (engine, controller)
            if k in per_run_violation:
                d = cohens_d(per_run_violation[k], per_run_violation[main_key])
                summary[k]["cohens_d_vs_main"] = d

    return summary


def print_and_export(summary: Dict, out_path: str):
    print(f"{'engine':<10} {'controller':<12} {'n':>3} "
          f"{'viol%':>8} {'95% CI':>18} {'CV%':>7} {'conv(s)':>8} "
          f"{'d vs main':>10}")
    lines_tex = []
    for (engine, controller), s in sorted(summary.items()):
        viol = s["violation_rate"] * 100 if not math.isnan(s["violation_rate"]) else float("nan")
        lo = s["violation_ci"][0] * 100
        hi = s["violation_ci"][1] * 100
        d = s.get("cohens_d_vs_main", float("nan"))
        print(f"{engine:<10} {controller:<12} {s['n_runs']:>3} "
              f"{viol:>7.2f}% [{lo:>5.2f}%,{hi:>5.2f}%] "
              f"{s['cv_percent']:>6.2f}% {s['mean_conv_time_s']:>7.2f} "
              f"{d:>10.2f}")

        d_str = f"{d:.2f}" if not math.isnan(d) else "--"
        lines_tex.append(
            f"{engine} & {controller} & {viol:.2f}\\% & "
            f"[{lo:.2f}\\%, {hi:.2f}\\%] & {s['cv_percent']:.2f}\\% & "
            f"{s['mean_conv_time_s']:.2f}\\,s & {d_str} \\\\"
        )

    with open(out_path, "w") as f:
        f.write("%% Auto-generated by analyze_baseline_results.py\n")
        f.write("%% Paste these rows into Table 4 (Section 5.8) in place\n")
        f.write("%% of the qualitative comparison, or into a new table.\n")
        f.write("\n".join(lines_tex) + "\n")
    print(f"\nLaTeX rows written to {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", help="Combined per-tick CSV "
                     "(engine,controller,run_id,t,rpo_star,rpo_hat)")
    ap.add_argument("--out", default="table4_rows.tex")
    args = ap.parse_args()

    grouped = load_runs(args.csv_path)
    summary = summarize(grouped)
    print_and_export(summary, args.out)


if __name__ == "__main__":
    main()
