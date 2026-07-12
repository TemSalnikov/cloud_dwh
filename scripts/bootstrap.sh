#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1 (192.168.31.195)
# ЗАЧЕМ:  развернуть платформу ТОЛЬКО из локальных пакетов (без интернета)
# ЗАПУСК: sudo bash scripts/bootstrap.sh
# ТРЕБУЕТ: sync-offline-bundle.sh выполнен с локальной машины
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
HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"

log() { echo "[bootstrap] $*"; }
die() { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

export PATH="/usr/local/bin:${HOME}/.local/bin:/snap/bin:${PATH}"

if [[ -n "${KUBECONFIG:-}" && -f "${KUBECONFIG}" ]]; then
  export KUBECONFIG
elif [[ -f /etc/kubernetes/admin.conf ]]; then
  export KUBECONFIG=/etc/kubernetes/admin.conf
fi

need() { command -v "$1" >/dev/null 2>&1 || die "Missing: $1 — запустите scripts/setup-node1.sh"; }
need kubectl
need helm

# ── preflight ────────────────────────────────────────────────

log "Checking offline bundle..."
if ! bash "$SCRIPT_DIR/verify-offline-bundle.sh"; then
  die "Offline-пакет неполный. На ЛОКАЛЬНОЙ машине с VPN:
  bash scripts/pack-offline-bundle.sh
  bash scripts/sync-offline-bundle.sh"
fi

if [[ ! -d "$REPO_ROOT/helm/platform/charts" ]] || ! ls "$REPO_ROOT/helm/platform/charts/"*.tgz &>/dev/null; then
  if ls "$VENDOR_DIR/platform-charts/"*.tgz &>/dev/null; then
    log "Restoring platform charts from vendor..."
    mkdir -p "$REPO_ROOT/helm/platform/charts"
    cp -a "$VENDOR_DIR/platform-charts/"*.tgz "$REPO_ROOT/helm/platform/charts/"
  fi
fi

# ── helpers ──────────────────────────────────────────────────

find_chart() {
  local name="$1"
  local f
  f=$(ls -1 "${CHARTS_DIR}/${name}"*.tgz 2>/dev/null | sort -V | tail -1)
  [[ -n "$f" && -f "$f" ]] && echo "$f" || return 1
}

release_deployed() {
  local release="$1" namespace="$2"
  local status
  status=$(helm status "$release" -n "$namespace" 2>/dev/null | awk '/^STATUS:/{print $2}')
  [[ "$status" == "deployed" ]]
}

helm_install_local() {
  local release="$1" chart_name="$2" namespace="$3"
  shift 3
  local extra_args=("$@")
  local wait_flag="--wait"

  # monitoring: --wait часто зависает на hooks/CRD даже когда pods Ready
  if [[ "$release" == "monitoring" ]]; then
    wait_flag=""
  fi

  if release_deployed "$release" "$namespace"; then
    log "Skip $release — already deployed in $namespace"
    return 0
  fi

  local chart_path
  chart_path=$(find_chart "$chart_name") || \
    die "Chart '$chart_name' не найден в $CHARTS_DIR — выполните sync-offline-bundle.sh"

  log "Installing $release ← $(basename "$chart_path")"
  # shellcheck disable=SC2086
  helm upgrade --install "$release" "$chart_path" \
    --namespace "$namespace" --create-namespace \
    "${extra_args[@]}" \
    $wait_flag --timeout "$HELM_TIMEOUT"

  if [[ "$release" == "monitoring" ]]; then
    log "Waiting for monitoring pods (max 5m)..."
    kubectl wait --for=condition=ready pod \
      -l "release=monitoring" -n monitoring \
      --timeout=5m 2>/dev/null || true
    kubectl wait --for=condition=ready pod \
      -l "app.kubernetes.io/name=prometheus" -n monitoring \
      --timeout=5m 2>/dev/null || true
    kubectl get pods -n monitoring
  fi
}

kubectl_apply_local() {
  local manifest_file="$1"
  [[ -f "$manifest_file" ]] || die "Manifest не найден: $manifest_file"
  log "Applying $(basename "$manifest_file")"
  kubectl apply -f "$manifest_file"
}

# ── install ──────────────────────────────────────────────────

log "Creating platform namespace..."
kubectl create namespace platform --dry-run=client -o yaml | kubectl apply -f -

log "Installing local-path-provisioner..."
kubectl_apply_local "${MANIFESTS_DIR}/local-path-storage.yaml"
kubectl annotate storageclass local-path storageclass.kubernetes.io/is-default-class=true --overwrite 2>/dev/null || true

log "Installing nginx-ingress (bare-metal: hostNetwork 80/443)..."
helm_install_local ingress-nginx ingress-nginx ingress-nginx \
  -f "$REPO_ROOT/deploy/ingress-nginx/values-baremetal.yaml"

log "Installing cert-manager..."
helm_install_local cert-manager cert-manager cert-manager \
  --set crds.enabled=true \
  --set resources.requests.cpu=100m \
  --set resources.requests.memory=128Mi

kubectl apply -f "$REPO_ROOT/deploy/cert-manager/selfsigned-issuer.yaml"

log "Installing Altinity ClickHouse Operator..."
kubectl_apply_local "${MANIFESTS_DIR}/clickhouse-operator-install-bundle.yaml"
kubectl wait --for=condition=available deployment/clickhouse-operator -n kube-system --timeout=5m 2>/dev/null || \
  kubectl wait --for=condition=available deployment/clickhouse-operator -n clickhouse --timeout=5m 2>/dev/null || true

log "Installing Strimzi Kafka Operator..."
helm_install_local strimzi strimzi-kafka-operator strimzi \
  -f "$REPO_ROOT/deploy/strimzi/values.yaml"

log "Installing CloudNativePG Operator..."
helm_install_local cnpg cloudnative-pg cnpg-system \
  --set resources.requests.cpu=100m \
  --set resources.requests.memory=256Mi \
  --set resources.limits.cpu=500m \
  --set resources.limits.memory=512Mi

log "Installing kube-prometheus-stack..."
helm_install_local monitoring kube-prometheus-stack monitoring \
  -f "$REPO_ROOT/deploy/monitoring/values-minimal.yaml"

log "Installing platform control plane..."
if release_deployed platform platform; then
  log "Skip platform — already deployed"
else
  helm upgrade --install platform "$REPO_ROOT/helm/platform" \
    --namespace platform \
    -f "$REPO_ROOT/helm/platform/values.yaml" \
    --wait --timeout "$HELM_TIMEOUT" || {
      log "WARN: platform helm wait failed — check pods:"
      kubectl get pods -n platform
      die "platform install failed"
    }
fi

log ""
log "=== Bootstrap status ==="
helm list -A
log ""
log "Bootstrap complete for $SERVER_IP"
log "Platform UI: https://platform.${BASE_DOMAIN:-192.168.31.195.nip.io}"
log "Grafana:     kubectl port-forward -n monitoring svc/monitoring-grafana 3001:80"
