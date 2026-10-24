"""
Analyse de l'etude d'ablation - 4 variantes vs PI complet (baseline).

Usage:
    python analyze_ablation.py
"""
import json
import glob
import numpy as np
from collections import defaultdict

SS_START = 300
RPO_STAR = 0.5
TOLERANCE = 0.10


def analyze_run(record):
    ticks = record["ticks"]
    all_rpo = np.array([tk["rpo_hat"] for tk in ticks], dtype=float)
    all_t = np.array([tk["t"] for tk in ticks], dtype=float)

    ss_mask = all_t > SS_START
    ss_rpo = all_rpo[ss_mask]
    ss_mu = float(np.mean(ss_rpo)) if len(ss_rpo) > 0 else float("nan")
    ss_sigma = float(np.std(ss_rpo)) if len(ss_rpo) > 0 else float("nan")

    viol_rate = float(np.mean(all_rpo > RPO_STAR)) * 100.0

    tol = TOLERANCE * RPO_STAR
    in_band = np.abs(all_rpo - RPO_STAR) <= tol
    post_warmup = all_t > 10.0
    cand = np.where(in_band & post_warmup)[0]
    conv_time = float(all_t[cand[0]]) if len(cand) > 0 else float("nan")

    return ss_mu, ss_sigma, viol_rate, conv_time


by_variant = defaultdict(list)
for f in sorted(glob.glob("results_ablation/*.json")):
    d = json.load(open(f))
    by_variant[d["variant"]].append(analyze_run(d))

print(f"\n{'Variant':<18} {'SS mu (s)':>10} {'SS sigma':>10} {'Viol %':>8} {'Conv (s)':>10} {'CV %':>7}")
print("-" * 68)
for variant, runs in by_variant.items():
    mus = [r[0] for r in runs]
    sigmas = [r[1] for r in runs]
    viols = [r[2] for r in runs]
    convs = [r[3] for r in runs if not np.isnan(r[3])]

    mu_mean = np.mean(mus)
    cv_pct = np.std(mus) / mu_mean * 100.0 if mu_mean != 0 else float("nan")

    print(f"{variant:<18} {mu_mean:>10.4f} {np.mean(sigmas):>10.4f} "
          f"{np.mean(viols):>7.1f}% {np.mean(convs) if convs else float('nan'):>10.2f} "
          f"{cv_pct:>6.1f}%")

print("\nReference (full PI, from main campaign, Table 5):")
print(f"{'full_PI':<18} {0.506:>10.4f} {0.001:>10.4f} {61.0:>7.1f}% {12.89:>10.2f} {0.3:>6.1f}%")
