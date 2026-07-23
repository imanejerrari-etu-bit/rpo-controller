#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_cluster.sh — Full cluster setup on WSL2 + Kind
#
# What this does:
#   1. Installs prerequisites (Kind, kubectl, Helm) if missing
#   2. Creates a 4-node Kind cluster
#   3. Deploys MongoDB, MySQL PXC, Redis
#   4. Waits for all pods to be ready
#   5. Sets up port-forwarding so the controller (running locally) can connect
#
# Usage:
#   chmod +x scripts/setup_cluster.sh
#   ./scripts/setup_cluster.sh
#
# Requirements:
#   - WSL2 with Ubuntu 22.04 or later
#   - Docker Desktop running (or Docker in WSL2)
#   - Python 3.11+ with pip
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

CLUSTER_NAME="rpo-cluster"
K8S_DIR="$(cd "$(dirname "$0")/../k8s" && pwd)"

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
section() { echo -e "\n${GREEN}══════════════════════════════════════${NC}"; \
             echo -e "${GREEN}  $*${NC}"; \
             echo -e "${GREEN}══════════════════════════════════════${NC}"; }


# ── 1. Prerequisites ──────────────────────────────────────────────────────────
section "Step 1: Checking prerequisites"

install_if_missing() {
    local bin=$1; local install_cmd=$2
    if ! command -v "$bin" &>/dev/null; then
        warn "$bin not found — installing ..."
        eval "$install_cmd"
    else
        info "$bin is available: $(command -v "$bin")"
    fi
}

# Docker
if ! docker info &>/dev/null; then
    error "Docker is not running. Start Docker Desktop and re-run."
fi
info "Docker is running"

# Kind
install_if_missing kind \
    'curl -Lo /usr/local/bin/kind https://kind.sigs.k8s.io/dl/v0.22.0/kind-linux-amd64 \
     && chmod +x /usr/local/bin/kind'

# kubectl
install_if_missing kubectl \
    'curl -Lo /usr/local/bin/kubectl "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
     && chmod +x /usr/local/bin/kubectl'

# Python deps — create a venv to avoid "externally-managed-environment" error
VENV_DIR="$(cd "$(dirname "$0")/.." && pwd)/.venv"
if [ ! -d "$VENV_DIR" ]; then
    info "Creating Python virtual environment at .venv ..."
    python3 -m venv "$VENV_DIR"
fi
info "Installing Python dependencies into .venv ..."
"$VENV_DIR/bin/pip" install --quiet pymongo "mysql-connector-python" redis numpy
info "Activate the venv before running experiments:"
info "  source .venv/bin/activate"


# ── 2. Create Kind cluster ────────────────────────────────────────────────────
section "Step 2: Creating Kind cluster"

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    warn "Cluster '${CLUSTER_NAME}' already exists — skipping creation"
else
    info "Creating 4-node Kind cluster ..."
    kind create cluster --name "$CLUSTER_NAME" \
         --config "$K8S_DIR/kind-config.yaml"
    info "Cluster created"
fi

export KUBECONFIG
KUBECONFIG="$(kind get kubeconfig-path --name "$CLUSTER_NAME" 2>/dev/null \
              || kind get kubeconfig --name "$CLUSTER_NAME" | head -1)"
kubectl cluster-info --context "kind-${CLUSTER_NAME}"


# ── 3. Deploy MongoDB ─────────────────────────────────────────────────────────
section "Step 3: Deploying MongoDB"

kubectl apply -f "$K8S_DIR/mongodb.yaml"
info "Waiting for MongoDB pod to be ready ..."
kubectl wait deployment/mongodb \
    --for=condition=Available \
    --timeout=120s \
    --namespace=default
info "MongoDB ready"


# ── 4. Deploy MySQL PXC ───────────────────────────────────────────────────────
section "Step 4: Deploying MySQL PXC (3-node Galera)"

kubectl apply -f "$K8S_DIR/mysql-pxc.yaml"
info "Waiting for MySQL PXC pods (this takes ~60 s for Galera bootstrap) ..."
kubectl rollout status statefulset/mysql-pxc \
    --timeout=240s \
    --namespace=default
info "MySQL PXC ready"

# Wait for bootstrap job
kubectl wait job/mysql-bootstrap \
    --for=condition=Complete \
    --timeout=120s \
    --namespace=default 2>/dev/null || warn "Bootstrap job may still be running"


# ── 5. Deploy Redis ───────────────────────────────────────────────────────────
section "Step 5: Deploying Redis"

kubectl apply -f "$K8S_DIR/redis.yaml"
info "Waiting for Redis pods ..."
kubectl rollout status statefulset/redis \
    --timeout=120s \
    --namespace=default
info "Redis ready"


# ── 6. Verify all pods ────────────────────────────────────────────────────────
section "Step 6: Pod status"
kubectl get pods -n default -o wide


# ── 7. Port-forwarding ────────────────────────────────────────────────────────
section "Step 7: Port-forwarding"

info "Starting port-forward for MongoDB  (localhost:27017) ..."
kubectl port-forward svc/mongodb 27017:27017 -n default &>/tmp/pf_mongo.log &
PF_MONGO=$!

info "Starting port-forward for MySQL    (localhost:3306) ..."
kubectl port-forward svc/mysql-pxc 3306:3306 -n default &>/tmp/pf_mysql.log &
PF_MYSQL=$!

info "Starting port-forward for Redis    (localhost:6379) ..."
kubectl port-forward svc/redis 6379:6379 -n default &>/tmp/pf_redis.log &
PF_REDIS=$!

sleep 5

# Test connections
info "Testing MongoDB connection ..."
"$VENV_DIR/bin/python3" -c "import pymongo; c=pymongo.MongoClient('mongodb://root:password@localhost:27017/admin',serverSelectionTimeoutMS=3000); c.admin.command('ping'); print('  MongoDB OK')"

info "Testing MySQL connection ..."
"$VENV_DIR/bin/python3" -c "import mysql.connector; c=mysql.connector.connect(host='localhost',port=3306,user='root',password='password',database='testdb'); c.close(); print('  MySQL OK')"

info "Testing Redis connection ..."
"$VENV_DIR/bin/python3" -c "import redis; r=redis.Redis(host='localhost',port=6379); r.ping(); print('  Redis OK')"


# ── Done ──────────────────────────────────────────────────────────────────────
section "Setup complete!"
echo ""
echo "  Port-forward PIDs: mongo=$PF_MONGO  mysql=$PF_MYSQL  redis=$PF_REDIS"
echo "  (These will stop when this shell exits)"
echo ""
echo "  To run the experiment campaign:"
echo "    cd $(dirname "$0")/.."
echo "    source .venv/bin/activate"
echo "    python3 experiments/run_campaign.py --n-runs 20"
echo ""
echo "  To analyze results:"
echo "    python3 analysis/analyze.py results/"
echo ""

# Keep port-forwards alive until Ctrl+C
trap "kill $PF_MONGO $PF_MYSQL $PF_REDIS 2>/dev/null; echo 'Port-forwards stopped'" EXIT
wait
