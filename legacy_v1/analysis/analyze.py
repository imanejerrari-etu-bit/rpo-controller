"""
Statistical Analysis — reproduces Tables 2–4 and bootstrap CIs from the paper.

Usage:
    python analyze.py results/
    python analyze.py results/ --engine mongodb
    python analyze.py results/ --latex          # output LaTeX table fragment
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from rpo_controller.config import SS_START, BOOTSTRAP_N, TOLERANCE


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap CI
# ─────────────────────────────────────────────────────────────────────────────

def bootstrap_ci(values: List[float], stat=np.mean,
                 n: int = BOOTSTRAP_N,
                 alpha: float = 0.05,
                 seed: int = 0) -> Tuple[float, float]:
    """
    Percentile bootstrap confidence interval.

    Returns (lo, hi) for (1-alpha) CI.
    """
    rng  = np.random.default_rng(seed)
    arr  = np.array(values, dtype=float)
    boot = [stat(rng.choice(arr, size=len(arr), replace=True))
            for _ in range(n)]
    lo = float(np.percentile(boot, 100 * alpha / 2))
    hi = float(np.percentile(boot, 100 * (1 - alpha / 2)))
    return lo, hi


# ─────────────────────────────────────────────────────────────────────────────
# Per-run statistics
# ─────────────────────────────────────────────────────────────────────────────

def analyze_run(record: Dict) -> Dict:
    """Compute per-run statistics from raw tick data."""
    ticks    = record["ticks"]
    rpo_star = record["rpo_star"]

    # All ticks
    all_rpo = np.array([tk["rpo_hat"] for tk in ticks], dtype=float)
    all_t   = np.array([tk["t"]       for tk in ticks], dtype=float)

    # Steady-state ticks (t > SS_START)
    ss_mask = all_t > SS_START
    ss_rpo  = all_rpo[ss_mask]

    ss_mu    = float(np.mean(ss_rpo))    if len(ss_rpo) > 0 else float("nan")
    ss_sigma = float(np.std(ss_rpo))     if len(ss_rpo) > 0 else float("nan")

    # Violation rate over full run (fraction of ticks with rpo_hat > rpo_star)
    viol_mask = all_rpo > rpo_star
    viol_rate = float(np.mean(viol_mask)) * 100.0   # percent

    # Convergence time: first t after warmup where |rpo_hat − rpo*| ≤ 10% rpo*
    tol      = TOLERANCE * rpo_star
    in_band  = np.abs(all_rpo - rpo_star) <= tol
    # Only consider t > warmup (10 s)
    post_warmup = all_t > 10.0
    cand = np.where(in_band & post_warmup)[0]
    conv_time = float(all_t[cand[0]]) if len(cand) > 0 else float("nan")

    return {
        "run_id":     record["run_id"],
        "tps":        record["tps"],
        "ss_mu":      ss_mu,
        "ss_sigma":   ss_sigma,
        "viol_pct":   viol_rate,
        "conv_time":  conv_time,
        "n_ticks":    len(ticks),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate over all runs for one engine
# ─────────────────────────────────────────────────────────────────────────────

def aggregate(runs: List[Dict]) -> Dict:
    """Aggregate per-run stats into summary with bootstrap CIs."""
    ss_mus    = [r["ss_mu"]    for r in runs if not np.isnan(r["ss_mu"])]
    viol_pcts = [r["viol_pct"] for r in runs]
    conv_times = [r["conv_time"] for r in runs
                  if not np.isnan(r["conv_time"])]

    # Inter-run CV on steady-state mean RPO
    cv_pct = (float(np.std(ss_mus))  / float(np.mean(ss_mus)) * 100.0
              if len(ss_mus) > 1 else float("nan"))

    # Bootstrap CI on violation rate
    viol_ci  = bootstrap_ci(viol_pcts)
    # Bootstrap CI on ss_mu
    ssmu_ci  = bootstrap_ci(ss_mus) if len(ss_mus) >= 2 else (float("nan"), float("nan"))
    # Bootstrap CI on convergence time
    conv_ci  = bootstrap_ci(conv_times) if len(conv_times) >= 2 else (float("nan"), float("nan"))

    return {
        "n_runs":         len(runs),
        "ss_mu_mean":     float(np.mean(ss_mus))     if ss_mus    else float("nan"),
        "ss_mu_std":      float(np.std(ss_mus))      if ss_mus    else float("nan"),
        "ss_mu_ci":       ssmu_ci,
        "cv_pct":         cv_pct,
        "viol_pct_mean":  float(np.mean(viol_pcts)),
        "viol_pct_ci":    viol_ci,
        "conv_time_mean": float(np.mean(conv_times)) if conv_times else float("nan"),
        "conv_time_ci":   conv_ci,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Load results from directory
# ─────────────────────────────────────────────────────────────────────────────

def load_results(results_dir: Path,
                 engine: Optional[str] = None) -> Dict[str, List[Dict]]:
    """Load all JSON result files, grouped by engine."""
    by_engine: Dict[str, List[Dict]] = {}
    pattern = f"{engine}_*.json" if engine else "*.json"

    for fpath in sorted(results_dir.glob(pattern)):
        try:
            rec = json.loads(fpath.read_text())
            eng = rec["engine"]
            by_engine.setdefault(eng, []).append(rec)
        except Exception as exc:
            print(f"Warning: could not load {fpath}: {exc}", file=sys.stderr)

    return by_engine


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def print_table(engine: str, runs: List[Dict], agg: Dict,
                latex: bool = False):
    per_run = [analyze_run(r) for r in runs]
    rpo_star = runs[0]["rpo_star"]

    if latex:
        _print_latex(engine, per_run, agg, rpo_star)
    else:
        _print_text(engine, per_run, agg, rpo_star)


def _print_text(engine, per_run, agg, rpo_star):
    print(f"\n{'═'*65}")
    print(f"  {engine.upper()}  (RPO* = {rpo_star} s)  —  {agg['n_runs']} runs")
    print(f"{'═'*65}")
    print(f"{'Run':>4}  {'TPS':>5}  {'SS μ(s)':>8}  {'SS σ(s)':>8}  "
          f"{'Viol%':>6}  {'Conv(s)':>8}")
    print(f"{'─'*65}")
    for r in per_run:
        print(f"{r['run_id']:>4}  {r['tps']:>5.0f}  "
              f"{r['ss_mu']:>8.3f}  {r['ss_sigma']:>8.3f}  "
              f"{r['viol_pct']:>5.1f}%  "
              f"{r['conv_time']:>8.2f}")
    print(f"{'─'*65}")
    ci_lo, ci_hi = agg["viol_pct_ci"]
    print(f"{'ALL':>4}  {'—':>5}  "
          f"{agg['ss_mu_mean']:>8.3f}  {agg['ss_mu_std']:>8.3f}  "
          f"{agg['viol_pct_mean']:>5.1f}%  "
          f"{agg['conv_time_mean']:>8.2f}")
    print(f"\n  Inter-run CV:      {agg['cv_pct']:.1f}%")
    print(f"  Violation 95% CI:  [{ci_lo:.1f}%, {ci_hi:.1f}%]")
    sslo, sshi = agg["ss_mu_ci"]
    print(f"  SS μ 95% CI:       [{sslo:.3f}, {sshi:.3f}] s")
    clo, chi = agg["conv_time_ci"]
    print(f"  Conv. time CI:     [{clo:.2f}, {chi:.2f}] s")


def _print_latex(engine, per_run, agg, rpo_star):
    """Output a LaTeX tabular body that can be pasted into the paper."""
    print(f"\n% ── {engine.upper()} (RPO* = {rpo_star} s) ──────────────")
    print(r"\midrule")
    for r in per_run:
        conv = f"{r['conv_time']:.2f}" if not (r['conv_time'] != r['conv_time']) else "---"
        print(f"  {r['run_id']} & {r['tps']:.0f} & {r['ss_mu']:.3f} & "
              f"{r['ss_sigma']:.3f} & {r['viol_pct']:.1f} & {conv} \\\\")
    print(r"\midrule")
    ci_lo, ci_hi = agg["viol_pct_ci"]
    sslo, sshi   = agg["ss_mu_ci"]
    print(f"  \\textbf{{All}} & -- & ${agg['ss_mu_mean']:.3f}$ & "
          f"${agg['ss_mu_std']:.3f}^{{\\dagger}}$ & "
          f"$\\mathbf{{{agg['viol_pct_mean']:.1f}}}$ & "
          f"CI [{sslo:.3f},\\,{sshi:.3f}] \\\\")
    print(f"% CV={agg['cv_pct']:.1f}%  Viol CI=[{ci_lo:.1f}%,{ci_hi:.1f}%]")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze RPO controller results")
    parser.add_argument("results_dir", type=Path,
                        help="Directory containing JSON result files")
    parser.add_argument("--engine", choices=["mongodb", "mysql", "redis"],
                        help="Analyze single engine only")
    parser.add_argument("--latex", action="store_true",
                        help="Output LaTeX table fragments")
    args = parser.parse_args()

    by_engine = load_results(args.results_dir, args.engine)

    if not by_engine:
        print("No result files found.", file=sys.stderr)
        sys.exit(1)

    for engine, runs in sorted(by_engine.items()):
        per_run = [analyze_run(r) for r in runs]
        agg     = aggregate(per_run)
        print_table(engine, runs, agg, latex=args.latex)

    print()
