# RPO Controller — Experiment Replication Package

PI adaptive write-behind controller for Kubernetes databases.
Companion code for the paper *"An Adaptive Write-Behind Interval Controller
for Kubernetes Databases: SLA-Driven RPO Management via PI Feedback Control"*.

---

## Citation

If you use this code, please cite:

> Jerrari I, Assayad I. "An Adaptive Write-Behind Interval Controller for
> Kubernetes Databases: SLA-Driven RPO Management via PI Feedback Control."
> *Computing* (Springer), [under revision].

---

## Repository Structure

```
rpo-controller/
├── rpo_controller/          # Core library
│   ├── config.py            # All constants from the paper (gains, thresholds)
│   ├── pi_controller.py     # PI control law — Algorithm 1, Eqs. 2-3
│   ├── proxies.py           # RPO proxies — Eqs. 4, 5, 6
│   ├── actuators.py         # Engine actuators (MongoDB / MySQL / Redis)
│   └── service.py           # Asyncio control loops
├── experiments/
│   ├── workload.py          # Token-bucket workload generators
│   ├── run_experiment.py    # Single run (with Redis BGREWRITEAOF fix)
│   └── run_campaign.py      # Automated 20-run campaign
├── analysis/
│   └── analyze.py           # Bootstrap CI, tables, LaTeX output
├── k8s/
│   ├── kind-config.yaml     # 4-node Kind cluster
│   ├── mongodb.yaml         # MongoDB 8.0
│   ├── mysql-pxc.yaml       # MySQL PXC 3-node Galera
│   └── redis.yaml           # Redis with AOF
├── scripts/
│   └── setup_cluster.sh     # Full automated setup (WSL2)
├── results/                 # Raw experiment output (JSON/CSV, one file per run)
├── analysis_outputs/        # Bootstrap CI tables, LaTeX fragments used in the paper
└── requirements.txt
```

---

## Prerequisites (WSL2 Ubuntu)

```bash
# 1. Docker Desktop running (with WSL2 integration enabled)

# 2. Install Python deps
pip install -r requirements.txt

# 3. Install Kind + kubectl (done automatically by setup_cluster.sh)
```

---

## Step 1 — Create Cluster & Deploy Databases

```bash
chmod +x scripts/setup_cluster.sh
./scripts/setup_cluster.sh
```

This will:
- Create a 4-node Kind cluster
- Deploy MongoDB 8.0, MySQL PXC (3-node Galera), Redis 8 with AOF
- Start port-forwarding on localhost:27017 / 3306 / 6379
- Run connection tests

**Keep this terminal open** — it maintains the port-forwards.

---

## Step 2 — Run the Campaign (20 runs × 3 engines)

Open a second terminal:

```bash
# All engines, 20 runs each (~10 hours total)
python experiments/run_campaign.py --n-runs 20

# Or single engine
python experiments/run_campaign.py --engine mongodb --n-runs 20

# Or resume from run 6 (if runs 1-5 already done)
python experiments/run_campaign.py --start-run 6 --n-runs 15
```

Results are saved to `results/` as JSON files, one per run.

### RPO targets tested per engine

The paper evaluates each engine under one or more RPO\* targets. The
campaign script exposes all of them via `--rpo-target`:

| Engine    | Regime                          | RPO\*        | $K_p$ | $K_i$ | Runs        |
|-----------|----------------------------------|--------------|------|------|-------------|
| MongoDB   | Regime 1 — Conservative           | 2.0 s        | 0.8  | 0.20 | 20 × 600 s  |
| MongoDB   | Regime 2 — Active tracking        | 0.5 s        | 0.8  | 0.20 | 10 × 600 s  |
| MongoDB   | Multi-target generalisation study | 1.0 / 2.0 / 3.0 s | 0.8  | 0.20 | 5 × 600 s each |
| MySQL PXC | Main evaluation                   | 1.0 s        | 0.5  | 0.10 | 20 × 600 s  |
| Redis     | Main evaluation                   | 1.0 s        | 0.4  | 0.08 | 20 × 600 s  |

The headline abstract figures (0.5 % / 0.3 % / 49.5 % violation rates,
1.2 % tracking accuracy) come from the specific regime/target listed
above — see `analysis_outputs/` for the exact table each number is
drawn from.

```bash
# Example: MongoDB Regime 2 (active tracking)
python experiments/run_campaign.py --engine mongodb --rpo-target 0.5 --n-runs 10
```

---

## Step 3 — Analyze Results

```bash
# Full summary with bootstrap CIs
python analysis/analyze.py results/

# LaTeX table fragments (paste directly into the paper)
python analysis/analyze.py results/ --latex

# Single engine
python analysis/analyze.py results/ --engine mongodb
```

---

## Key Implementation Notes

### Redis BGREWRITEAOF fix
The paper acknowledges that Run 1 had 100% violations due to a cold-start
AOF artifact. `run_experiment.py` calls `BGREWRITEAOF` and waits for
completion before every run. This is critical for reproducible results.

### MongoDB multi-target experiments
The campaign also runs MongoDB at RPO* ∈ {1.0, 2.0, 3.0} s to demonstrate
controller generalization across SLA targets.

---

## Oracle Cloud Free Tier (for multi-machine cluster)

To run on a real multi-machine cluster (addresses Threat T1):

```bash
# 1. Create 4 ARM instances on Oracle Cloud Free Tier
#    (Always Free: 4 × Ampere A1, 1 OCPU + 6 GB RAM each)

# 2. On each node, install k3s
# Master node:
curl -sfL https://get.k3s.io | sh -
# Get the token:
sudo cat /var/lib/rancher/k3s/server/node-token

# Worker nodes:
curl -sfL https://get.k3s.io | \
  K3S_URL=https://<MASTER_IP>:6443 \
  K3S_TOKEN=<TOKEN> sh -

# 3. Copy kubeconfig from master to your machine
scp user@master:/etc/rancher/k3s/k3s.yaml ~/.kube/config
sed -i 's/127.0.0.1/<MASTER_IP>/g' ~/.kube/config

# 4. Deploy databases same as Kind
kubectl apply -f k8s/mongodb.yaml
kubectl apply -f k8s/mysql-pxc.yaml
kubectl apply -f k8s/redis.yaml

# 5. Run experiments from your machine (with port-forwarding)
kubectl port-forward svc/mongodb 27017:27017 &
kubectl port-forward svc/mysql-pxc 3306:3306 &
kubectl port-forward svc/redis 6379:6379 &
python experiments/run_campaign.py --n-runs 20
```

Inter-node latency on Oracle Cloud: ~1–5 ms (vs. ~0.1 ms on Kind/WSL2),
directly addressing Threat T1 from the paper's validity section.

---

## Data availability

Raw per-run results (JSON) and derived analysis tables (CSV/LaTeX) are
included under `results/` and `analysis_outputs/` respectively. If the
full raw dataset exceeds GitHub's practical size limits, it is archived
separately with a permanent DOI — see the link in the paper's Data
Availability statement.
