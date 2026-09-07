# =====================================================================
# Repo reorganization script (Reviewer 1, point 5)
#
# Run this from INSIDE rpo-controller-github (the fresh clone), from
# the repo root. It uses "git mv" so history is preserved for each
# file instead of being lost via a plain Move-Item + git add.
# =====================================================================

$ErrorActionPreference = "Stop"

# --- 1. Create the target folders -------------------------------------
New-Item -ItemType Directory -Force -Path "rpo_controller" | Out-Null
New-Item -ItemType Directory -Force -Path "experiments" | Out-Null
New-Item -ItemType Directory -Force -Path "analysis" | Out-Null
New-Item -ItemType Directory -Force -Path "k8s" | Out-Null
New-Item -ItemType Directory -Force -Path "scripts" | Out-Null
New-Item -ItemType Directory -Force -Path "results" | Out-Null

# --- 2. Core library -> rpo_controller/ --------------------------------
$coreFiles = @(
    "config.py", "pi_controller.py", "baseline_controllers.py",
    "proxies.py", "actuators.py", "service.py", "service_baseline.py",
    "__init__.py"
)
foreach ($f in $coreFiles) {
    if (Test-Path $f) { git mv $f "rpo_controller/$f" }
    else { Write-Warning "Not found, skipping: $f" }
}

# --- 3. Experiment drivers + workload generator -> experiments/ -------
$experimentFiles = @(
    "workload.py", "run_experiment.py", "run_experiment_baseline.py",
    "run_campaign.py", "run_campaign_ycsb.py", "run_experiment_ycsb.py",
    "run_ablation.py", "run_baseline_experiment.py", "run_bursty_and_baseline.py",
    "run_dithering.py", "run_sensitivity.py", "dithering.py",
    "bursty_workload.py"
)
foreach ($f in $experimentFiles) {
    if (Test-Path $f) { git mv $f "experiments/$f" }
    else { Write-Warning "Not found, skipping: $f" }
}
# experiments/ needs its own __init__.py to be importable as a package
if (-not (Test-Path "experiments/__init__.py")) {
    New-Item -ItemType File -Path "experiments/__init__.py" | Out-Null
    git add "experiments/__init__.py"
}

# --- 4. Analysis scripts -> analysis/ ----------------------------------
$analysisFiles = @("analyze.py", "analyze_ablation.py", "analyze_baseline_results.py")
foreach ($f in $analysisFiles) {
    if (Test-Path $f) { git mv $f "analysis/$f" }
    else { Write-Warning "Not found, skipping: $f" }
}

# --- 5. Kubernetes manifests -> k8s/ -----------------------------------
$k8sFiles = @("kind-config.yaml", "mongodb.yaml", "mysql-pxc.yaml", "redis.yaml")
foreach ($f in $k8sFiles) {
    if (Test-Path $f) { git mv $f "k8s/$f" }
    else { Write-Warning "Not found, skipping: $f" }
}

# --- 6. Setup script -> scripts/ ---------------------------------------
if (Test-Path "setup_cluster.sh") { git mv "setup_cluster.sh" "scripts/setup_cluster.sh" }

# --- 7. Everything else that looks like experiment OUTPUT -> results/ --
# (json result files, .log files, sensitivity_summary.json, tps_sequence.txt)
$resultPatterns = @("*.json", "*.log", "tps_sequence.txt")
foreach ($pattern in $resultPatterns) {
    Get-ChildItem -Path . -Filter $pattern -File | ForEach-Object {
        git mv $_.Name "results/$($_.Name)"
    }
}

Write-Host ""
Write-Host "=== Reorganization complete. Review before committing: ===" -ForegroundColor Cyan
git status

Write-Host ""
Write-Host "=== STILL MISSING (Reviewer 1, point 6) -- create these before pushing: ===" -ForegroundColor Yellow
Write-Host "  rpo_controller/mpc_controller.py   -- imported by service.py, absent from the repo entirely"
Write-Host "  rpo_controller/predictors.py       -- imported by mpc_controller.py, also absent"
Write-Host ""
Write-Host "If you have these files locally (from tonight's uploads), copy them in:"
Write-Host '  Copy-Item "<path-to-your-local-mpc_controller.py>" rpo_controller\mpc_controller.py'
Write-Host '  Copy-Item "<path-to-your-local-predictors.py>" rpo_controller\predictors.py'
Write-Host "  git add rpo_controller/mpc_controller.py rpo_controller/predictors.py"
Write-Host ""
Write-Host "Once everything looks right in 'git status', commit and push:"
Write-Host "  git add -A"
Write-Host "  git commit -m `"Reorganize repo into documented package structure (rpo_controller/, experiments/, analysis/, k8s/, scripts/, results/); fixes README path mismatch (Reviewer 1, point 5)`""
Write-Host "  git push"
