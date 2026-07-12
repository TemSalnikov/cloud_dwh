#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1 (192.168.31.195)
# ЗАЧЕМ:  развернуть ingress, operators, monitoring, platform
#         использует deploy/vendor/ (offline) если есть
# ЗАПУСК: sudo bash /home/user/dev/cloud_dwh/scripts/bootstrap.sh
# ТРЕБУЕТ: setup-node1.sh, build-images.sh, download-vendor.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENDOR_DIR="$REPO_ROOT/deploy/vendor"
CHARTS_DIR="$VENDOR_DIR/charts"
MANIFESTS_DIR="$VENDOR_DIR/manifests"

if [[ -f "$REPO_ROOT/deploy/server.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/deploy/server.env"
  set +a
fi

if [[ -f "$VENDOR_DIR/versions.env" ]]; then
  # shellcheck source=/dev/null
  source "$VENDOR_DIR/versions.env"
fi

SERVER_IP="${SERVER_IP:-192.168.31.195}"
HELM_TIMEOUT="${HELM_TIMEOUT:-20m}"

log() { echo "[bootstrap] $*"; }
warn() { echo "[bootstrap] WARN: $*" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1 — запустите scripts/setup-node1.sh"; exit 1; }; }

export PATH="/usr/local/bin:${HOME}/.local/bin:/snap/bin:${PATH}"

if [[ -n "${KUBECONFIG:-}" && -f "${KUBECONFIG}" ]]; then
  export KUBECONFIG
elif [[ -f /etc/kubernetes/admin.conf ]]; then
  export KUBECONFIG=/etc/kubernetes/admin.conf
fi

need kubectl
need helm

# ── helpers ──────────────────────────────────────────────────

find_chart() {
  local name="$1"
  local f
  f=$(ls -1 "${CHARTS_DIR}/${name}"*.tgz 2>/dev/null | sort -V | tail -1)
  [[ -n "$f" && -f "$f" ]] && echo "$f" || return 1
}

helm_install() {
  local release="$1" chart_spec="$2" namespace="$3"
  shift 3
  local extra_args=("$@")

  local chart_name="${chart_spec%%/*}"
  chart_name="${chart_name##*/}"
  chart_name="${chart_name%%:*}"

  local chart_path=""
  if chart_path=$(find_chart "$chart_name"); then
    log "Installing $release from local vendor: $chart_path"
    helm upgrade --install "$release" "$chart_path" \
      --namespace "$namespace" --create-namespace \
      "${extra_args[@]}" \
      --wait --timeout "$HELM_TIMEOUT"
  else
    warn "Local chart not found for $chart_name — trying online (may timeout!)"
    warn "Run: bash scripts/download-vendor.sh"
    local repo_name="${chart_spec%%/*}"
    local repo_url=""
    case "$repo_name" in
      ingress-nginx) repo_url="$REPO_INGRESS_NGINX" ;;
      jetstack)      repo_url="$REPO_JETSTACK" ;;
      strimzi)       repo_url="$REPO_STRIMZI" ;;
      cnpg)          repo_url="$REPO_CNPG" ;;
      prometheus-community) repo_url="$REPO_PROMETHEUS" ;;
    esac
    [[ -n "$repo_url" ]] && helm repo add "$repo_name" "$repo_url" 2>/dev/null || true
    helm repo update "$repo_name" 2>/dev/null || helm repo update
    helm upgrade --install "$release" "$chart_spec" \
      --namespace "$namespace" --create-namespace \
      "${extra_args[@]}" \
      --wait --timeout "$HELM_TIMEOUT"
  fi
}

kubectl_apply() {
  local manifest_file="$1" url="$2"
  if [[ -f "$manifest_file" ]]; then
    log "Applying local manifest: $manifest_file"
    kubectl apply -f "$manifest_file"
  else
    warn "Local manifest missing: $manifest_file — trying URL"
    warn "Run: bash scripts/download-vendor.sh"
    kubectl apply -f "$url"
  fi
}

# ── preflight ────────────────────────────────────────────────

if [[ ! -d "$CHARTS_DIR" ]] || [[ -z "$(ls -A "$CHARTS_DIR" 2>/dev/null)" ]]; then
  warn "deploy/vendor/charts/ пуст!"
  warn "На машине с интернетом выполните:"
  warn "  bash scripts/download-vendor.sh"
  warn "  rsync -avz deploy/vendor/ user@${SERVER_IP}:${REPO_ROOT}/deploy/vendor/"
  warn "Продолжаем с online-режимом (может упасть по timeout)..."
fi

# ── install ──────────────────────────────────────────────────

log "Creating platform namespace..."
kubectl create namespace platform --dry-run=client -o yaml | kubectl apply -f -

log "Installing local-path-provisioner..."
kubectl_apply "${MANIFESTS_DIR}/local-path-storage.yaml" "${MANIFEST_LOCAL_PATH:-https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.28/deploy/local-path-storage.yaml}"
kubectl annotate storageclass local-path storageclass.kubernetes.io/is-default-class=true --overwrite 2>/dev/null || true

log "Installing nginx-ingress..."
helm_install ingress-nginx "${CHART_INGRESS_NGINX:-ingress-nginx/ingress-nginx:4.15.1}" ingress-nginx \
  --set controller.resources.requests.cpu=200m \
  --set controller.resources.requests.memory=256Mi

log "Installing cert-manager..."
helm_install cert-manager "${CHART_CERT_MANAGER:-jetstack/cert-manager:v1.16.2}" cert-manager \
  --set crds.enabled=true \
  --set resources.requests.cpu=100m \
  --set resources.requests.memory=128Mi

kubectl apply -f "$REPO_ROOT/deploy/cert-manager/selfsigned-issuer.yaml"

log "Installing Altinity ClickHouse Operator..."
kubectl_apply "${MANIFESTS_DIR}/clickhouse-operator-install-bundle.yaml" \
  "${MANIFEST_CLICKHOUSE_OP:-https://raw.githubusercontent.com/Altinity/clickhouse-operator/master/deploy/operator/clickhouse-operator-install-bundle.yaml}"
kubectl wait --for=condition=available deployment/clickhouse-operator -n kube-system --timeout=5m 2>/dev/null || \
  kubectl wait --for=condition=available deployment/clickhouse-operator -n clickhouse --timeout=5m 2>/dev/null || true

log "Installing Strimzi Kafka Operator..."
helm_install strimzi "${CHART_STRIMZI:-strimzi/strimzi-kafka-operator:0.45.0}" strimzi \
  --set resources.requests.cpu=200m \
  --set resources.requests.memory=512Mi

log "Installing CloudNativePG Operator..."
helm_install cnpg "${CHART_CNPG:-cnpg/cloudnative-pg:0.23.2}" cnpg-system \
  --set resources.requests.cpu=100m \
  --set resources.requests.memory=256Mi

log "Installing kube-prometheus-stack (minimal)..."
helm_install monitoring "${CHART_PROMETHEUS:-prometheus-community/kube-prometheus-stack:67.5.0}" monitoring \
  -f "$REPO_ROOT/deploy/monitoring/values-minimal.yaml"

log "Updating platform chart dependencies..."
cd "$REPO_ROOT/helm/platform"
if [[ -d charts ]] && ls charts/*.tgz &>/dev/null; then
  log "Using cached platform dependencies"
else
  helm repo add bitnami https://charts.bitnami.com/bitnami 2>/dev/null || true
  helm dependency update 2>/dev/null || warn "platform dependency update failed — run download-vendor.sh"
fi
cd "$REPO_ROOT"

log "Installing platform control plane..."
helm upgrade --install platform "$REPO_ROOT/helm/platform" \
  --namespace platform \
  -f "$REPO_ROOT/helm/platform/values.yaml" \
  --wait --timeout "$HELM_TIMEOUT"

log "Bootstrap complete for server $SERVER_IP."
log "Platform UI: https://platform.${BASE_DOMAIN:-192.168.31.195.nip.io}"
log "Grafana:     https://grafana.${BASE_DOMAIN:-192.168.31.195.nip.io}"
log "Or port-forward: kubectl port-forward -n platform svc/platform-ui 3000:80"
