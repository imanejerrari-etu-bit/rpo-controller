# rpo-controller: replication package

Code, manifests and raw logs for the manuscript

> Jerrari I, Assayad I. *When Is a Database Durability Knob Controllable? A Proxy-Validity Audit of
> Closed-Loop Write-Behind Control on MySQL PXC, MongoDB and Redis.* Manuscript under review.

The release matching the manuscript is the Git tag `v1.0`.

## What this package does and does not show

It audits, in open loop, the signals used to observe a database durability setting, and evaluates a PI
controller on the two engines whose signals are valid (MySQL PXC, MongoDB). The signals measure the window of
journal data not yet synchronised to disk. **They were not verified by crash recovery, and no data-loss (RPO)
guarantee is claimed.** Redis exposes no independent signal and is reported as a negative case.

## Environment used in the study

| Component | Version / setting |
|---|---|
| Host | single Windows 11 host, WSL2 (Ubuntu), Docker Desktop |
| Python | 3.12.3, virtual environment `venv` created inside WSL |
| Kubernetes | Kind cluster, v1.27.3, 1 control-plane + 3 workers (`k8s/kind-config.yaml`) |
| MySQL | Percona Operator for MySQL (PXC) v1.20.0, PXC image 8.4.8-8.1, 3 PXC + 3 HAProxy pods, cluster name `pxc-rpo-experiment` (`k8s/pxc_cr.yaml`) |
| MongoDB | one pod, Deployment `mongodb`, authentication enabled, `wiredTigerCacheSizeGB=1`, initial `journalCommitInterval` 100 ms (`k8s/mongodb.yaml`) |
| Redis | one primary and two replicas, AOF enabled (`k8s/redis.yaml`) |
| Namespace | `default` |

## Layout

```
rpo_controller/      PI controller, configuration constants, first-version proxies (used by the audit)
experiments/         step-response, redo-gap probe, closed-loop controllers, overhead, generator check;
                     run_experiment.py and workload.py are helper modules shared with the first version
run_sweep.sh         MySQL PXC target sweep (4 targets x 3 repetitions, randomised order)
run_static.sh        MySQL static references (mode 1 and mode 2)
run_sweep_mongo.sh   MongoDB sweep and controller baselines (POLICY / PFX variables, see the script header)
run_load.sh          offered-load invariance at a 0.3 s target (one engine at a time)
analyze_*.py, check_error_identity.py, make_figdata.py   tables and figure data from results/
k8s/                 Kind configuration and manifests (passwords are placeholders: CHANGE_ME)
results/             raw per-tick logs and summaries used by the manuscript
figdata/             data behind the figures
legacy_v1/           code and results of the first version of this work (see below)
```

## Setup

```bash
# inside WSL (Ubuntu), from the repository root
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

kind create cluster --config k8s/kind-config.yaml        # cluster name: rpo-cluster (context kind-rpo-cluster)

# MongoDB: first replace the CHANGE_ME placeholders in the Secret of k8s/mongodb.yaml (root user and password)
kubectl apply -f k8s/mongodb.yaml

# MySQL PXC: operator v1.20.0, then the cluster resource
kubectl apply --server-side -f https://raw.githubusercontent.com/percona/percona-xtradb-cluster-operator/v1.20.0/deploy/bundle.yaml
kubectl apply -f k8s/pxc_cr.yaml

# Redis
kubectl apply -f k8s/redis.yaml
```

The Operator generates the PXC user passwords by default (`secretsName` is not set in `k8s/pxc_cr.yaml`, so the
Secret is named after the cluster; check with `kubectl get secrets`). Read the root password with
`kubectl get secret pxc-rpo-experiment-secrets -o jsonpath='{.data.root}' | base64 -d`. The scripts expect the
pods `pxc-rpo-experiment-pxc-0` to `-2`, so keep `metadata.name: pxc-rpo-experiment` in that file.

## Credentials and port-forwards

No credential is stored in this repository. Export them in the terminal that runs the experiments:

```bash
export MYSQL_PASS='<PXC root password>'
export MONGO_URI='mongodb://<user>:<password>@127.0.0.1:27017/?authSource=admin'   # root user/password set in the Secret of k8s/mongodb.yaml
# Redis: REDIS_PASS in rpo_controller/config.py is empty (no authentication)

# MySQL: one port-forward per PXC pod (experiments connect to each node)
kubectl port-forward pod/pxc-rpo-experiment-pxc-0 33061:3306 -n default &
kubectl port-forward pod/pxc-rpo-experiment-pxc-1 33062:3306 -n default &
kubectl port-forward pod/pxc-rpo-experiment-pxc-2 33063:3306 -n default &
# MongoDB
kubectl port-forward deploy/mongodb 27017:27017 &
```

## From the manuscript to the scripts

| Manuscript item | Script | Output |
|---|---|---|
| Table 2, proxy audit (MySQL, Redis) | `python -m experiments.step_response --engine mysql --tps 80 --hold 300`, `--engine redis --tps 400 --hold 300` | `results/step_<engine>_<tps>tps.json` |
| Table 3, MySQL redo gap | `python -m experiments.redo_gap_probe --tps 80 --hold 60` (also 60 and 100 TPS) | `results/redo_gap_<tps>tps.json` |
| Table 4, MongoDB flush period vs. model | `python -m experiments.step_response_mongo --tps 250 --hold 120` (also 100 and 400 TPS) | `results/step_mongo_<tps>tps.json` |
| Table 5, MySQL PXC tracking | `bash run_sweep.sh`, `bash run_static.sh`, then `python analyze_sweep.py` | `results/sweep_*.csv`, `results/cl_mysql_*.json` |
| Table 6, MongoDB tracking and cost | `bash run_sweep_mongo.sh`, then `python analyze_sweep_mongo.py` | `results/sweep_mongo_summary.csv` |
| Table 7, load invariance | `bash run_load.sh`, then `python analyze_load.py` | `results/load_summary.csv` |
| Table 8, baselines | `run_sweep_mongo.sh` with the POLICY / PFX variants, then `python analyze_sweep_mongo.py` | `results/sweep_mongo_summary.csv` |
| Table 9, error-accounting identity | `python check_error_identity.py` | `results/error_identity.csv` |
| Table 10, controller overhead | `experiments/measure_overhead.py` | `results/overhead_*.json` |
| Figures | `python make_figdata.py` | `figdata/` |

Arguments not shown above are documented in the header of each script.

## Regenerating the tables without a cluster

The analysis scripts read the logs in `results/`, so Tables 5 to 9 can be regenerated without any database:

```bash
python analyze_sweep.py
python analyze_sweep_mongo.py
python analyze_load.py
python check_error_identity.py
python make_figdata.py
```

## Known limitations of the data

- The token-bucket load generator used in all campaigns delivers more than its nominal rate at low rates. Nominal
  loads are labels; the acknowledged rates are what was measured. A corrected limiter was not used for these runs.
- All results come from one single-host Kind cluster, with n = 3 runs per cell.
- `legacy_v1/` holds the first version of this work, whose RPO proxies were shown to be invalid by the open-loop
  audit reported in the manuscript. It is kept for transparency and is **not** used for any result in the manuscript.

## Licence

See `LICENSE`.

## Note on shared modules

`rpo_controller/service.py`, `mpc_controller.py` and `predictors.py` belong to the controller library shared with other work from the same project. `experiments/run_experiment.py` imports `service`, so these modules must be present for the scripts to import, but they are not used for any result reported in the manuscript, which relies on the PI controller (`pi_controller.py`), the signal readers and the actuators.
