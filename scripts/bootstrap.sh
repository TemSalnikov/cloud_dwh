#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1 (192.168.31.195)
# ЗАЧЕМ:  развернуть ingress, operators, monitoring, platform
# ЗАПУСК: sudo bash /opt/cloud_dwh/scripts/bootstrap.sh
# ТРЕБУЕТ: setup-node1.sh и build-images.sh (выполнены ранее)
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Загрузить конфиг сервера
if [[ -f "$REPO_ROOT/deploy/server.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/deploy/server.env"
  set +a
fi

SERVER_IP="${SERVER_IP:-192.168.31.195}"

log() { echo "[bootstrap] $*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1 — запустите scripts/setup-node1.sh на сервере"; exit 1; }; }

# Пути: node1 (/usr/local/bin) или локальная машина (~/.local/bin, snap)
export PATH="/usr/local/bin:${HOME}/.local/bin:/snap/bin:${PATH}"

# На node1 kubeconfig из admin.conf
if [[ -n "${KUBECONFIG:-}" && -f "${KUBECONFIG}" ]]; then
  export KUBECONFIG
elif [[ -f /etc/kubernetes/admin.conf ]]; then
  export KUBECONFIG=/etc/kubernetes/admin.conf
fi

need kubectl
need helm

log "Creating platform namespace..."
kubectl create namespace platform --dry-run=client -o yaml | kubectl apply -f -

log "Installing local-path-provisioner..."
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.28/deploy/local-path-storage.yaml
kubectl annotate storageclass local-path storageclass.kubernetes.io/is-default-class=true --overwrite 2>/dev/null || true

log "Installing nginx-ingress..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true
helm repo update
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.resources.requests.cpu=200m \
  --set controller.resources.requests.memory=256Mi \
  --wait --timeout 5m

log "Installing cert-manager..."
helm repo add jetstack https://charts.jetstack.io 2>/dev/null || true
helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager --create-namespace \
  --set crds.enabled=true \
  --set resources.requests.cpu=100m \
  --set resources.requests.memory=128Mi \
  --wait --timeout 5m

kubectl apply -f "$REPO_ROOT/deploy/cert-manager/selfsigned-issuer.yaml"

log "Installing Altinity ClickHouse Operator..."
kubectl apply -f https://raw.githubusercontent.com/Altinity/clickhouse-operator/master/deploy/operator/clickhouse-operator-install-bundle.yaml
kubectl wait --for=condition=available deployment/clickhouse-operator -n kube-system --timeout=3m 2>/dev/null || \
  kubectl wait --for=condition=available deployment/clickhouse-operator -n clickhouse --timeout=3m 2>/dev/null || true

log "Installing Strimzi Kafka Operator..."
helm repo add strimzi https://strimzi.io/charts/ 2>/dev/null || true
helm upgrade --install strimzi strimzi/strimzi-kafka-operator \
  --namespace strimzi --create-namespace \
  --set resources.requests.cpu=200m \
  --set resources.requests.memory=512Mi \
  --wait --timeout 5m

log "Installing CloudNativePG Operator..."
helm repo add cnpg https://cloudnative-pg.github.io/charts 2>/dev/null || true
helm upgrade --install cnpg cnpg/cloudnative-pg \
  --namespace cnpg-system --create-namespace \
  --set resources.requests.cpu=100m \
  --set resources.requests.memory=256Mi \
  --wait --timeout 5m

log "Installing kube-prometheus-stack (minimal)..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f "$REPO_ROOT/deploy/monitoring/values-minimal.yaml" \
  --wait --timeout 10m

log "Installing platform control plane..."
helm upgrade --install platform "$REPO_ROOT/helm/platform" \
  --namespace platform \
  -f "$REPO_ROOT/helm/platform/values.yaml" \
  --wait --timeout 5m

log "Bootstrap complete for server $SERVER_IP."
log "Platform UI: https://platform.${BASE_DOMAIN:-192.168.31.195.nip.io}"
log "Grafana:     https://grafana.${BASE_DOMAIN:-192.168.31.195.nip.io}"
log "Or port-forward: kubectl port-forward -n platform svc/platform-ui 3000:80"
