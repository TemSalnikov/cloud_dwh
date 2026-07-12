#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: ЛОКАЛЬНАЯ (с VPN / интернетом)
# ЗАЧЕМ:  скачать Helm charts, manifests, platform dependencies
# ЗАПУСК: bash scripts/download-vendor.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENDOR_DIR="$REPO_ROOT/deploy/vendor"
CHARTS_DIR="$VENDOR_DIR/charts"
MANIFESTS_DIR="$VENDOR_DIR/manifests"
VENDOR_PLATFORM="$VENDOR_DIR/platform-charts"

export PATH="/usr/local/bin:${HOME}/.local/bin:/snap/bin:${PATH}"

# shellcheck source=/dev/null
source "$VENDOR_DIR/versions.env"

log() { echo "[download-vendor] $*"; }
retry() {
  local n=0 max=5 delay=15
  until "$@"; do
    n=$((n + 1))
    [[ $n -ge $max ]] && return 1
    log "Retry $n/$max in ${delay}s..."
    sleep "$delay"
    delay=$((delay + 10))
  done
}

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }
need helm
need curl

mkdir -p "$CHARTS_DIR" "$MANIFESTS_DIR" "$VENDOR_PLATFORM"

# Добавить все repos один раз
log "Adding helm repos..."
helm repo add ingress-nginx "$REPO_INGRESS_NGINX" --force-update 2>/dev/null || helm repo add ingress-nginx "$REPO_INGRESS_NGINX"
helm repo add jetstack "$REPO_JETSTACK" --force-update 2>/dev/null || helm repo add jetstack "$REPO_JETSTACK"
helm repo add strimzi "$REPO_STRIMZI" --force-update 2>/dev/null || helm repo add strimzi "$REPO_STRIMZI"
helm repo add cnpg "$REPO_CNPG" --force-update 2>/dev/null || helm repo add cnpg "$REPO_CNPG"
helm repo add prometheus-community "$REPO_PROMETHEUS" --force-update 2>/dev/null || helm repo add prometheus-community "$REPO_PROMETHEUS"
helm repo add bitnami https://charts.bitnami.com/bitnami --force-update 2>/dev/null || helm repo add bitnami https://charts.bitnami.com/bitnami
retry helm repo update

pull_chart() {
  local spec="$1"
  local repo_name="${spec%%/*}"
  local chart_ver="${spec##*:}"
  local chart_name="${spec#*/}"
  chart_name="${chart_name%%:*}"

  if ls "${CHARTS_DIR}/${chart_name}"*.tgz &>/dev/null; then
    log "Already exists: $(ls -1 "${CHARTS_DIR}/${chart_name}"*.tgz | tail -1)"
    return 0
  fi

  log "Pulling ${repo_name}/${chart_name} ${chart_ver}..."
  retry helm pull "${repo_name}/${chart_name}" \
    --version "$chart_ver" \
    --destination "$CHARTS_DIR"
  log "Saved: $(ls -1 "${CHARTS_DIR}/${chart_name}"*.tgz | tail -1)"
}

download_manifest() {
  local url="$1" filename="$2"
  local out="${MANIFESTS_DIR}/${filename}"
  [[ -f "$out" ]] && { log "Already exists: $out"; return 0; }
  log "Downloading $filename ..."
  retry curl -fsSL --connect-timeout 60 --max-time 600 "$url" -o "$out"
  log "Saved: $out"
}

log "=== Helm charts ==="
pull_chart "$CHART_INGRESS_NGINX"
pull_chart "$CHART_CERT_MANAGER"
pull_chart "$CHART_STRIMZI"
pull_chart "$CHART_CNPG"
pull_chart "$CHART_PROMETHEUS"

log "=== K8s manifests ==="
download_manifest "$MANIFEST_LOCAL_PATH" "local-path-storage.yaml"
download_manifest "$MANIFEST_CLICKHOUSE_OP" "clickhouse-operator-install-bundle.yaml"

log "=== Platform chart dependencies (bitnami) ==="
cd "$REPO_ROOT/helm/platform"
retry helm dependency update
cp -a charts/*.tgz "$VENDOR_PLATFORM/" 2>/dev/null || true
cd "$REPO_ROOT"

log "=== Итог ==="
ls -lh "$CHARTS_DIR/"
ls -lh "$MANIFESTS_DIR/"
ls -lh "$VENDOR_PLATFORM/" 2>/dev/null || true
du -sh "$VENDOR_DIR"

log ""
log "Далее:"
log "  bash scripts/verify-offline-bundle.sh"
log "  bash scripts/sync-offline-bundle.sh"
