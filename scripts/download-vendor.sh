#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: любая с нормальным интернетом (локальная или node1)
# ЗАЧЕМ:  скачать Helm charts и K8s manifests в deploy/vendor/
#         для offline bootstrap на node1
# ЗАПУСК: bash scripts/download-vendor.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENDOR_DIR="$REPO_ROOT/deploy/vendor"
CHARTS_DIR="$VENDOR_DIR/charts"
MANIFESTS_DIR="$VENDOR_DIR/manifests"

export PATH="/usr/local/bin:${HOME}/.local/bin:/snap/bin:${PATH}"

# shellcheck source=/dev/null
source "$VENDOR_DIR/versions.env"

log() { echo "[download-vendor] $*"; }
retry() {
  local n=0 max=3 delay=10
  until "$@"; do
    n=$((n + 1))
    [[ $n -ge $max ]] && return 1
    log "Retry $n/$max in ${delay}s..."
    sleep "$delay"
    delay=$((delay * 2))
  done
}

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }
need helm
need curl

mkdir -p "$CHARTS_DIR" "$MANIFESTS_DIR"

pull_chart() {
  local spec="$1"   # repo/chart:version
  local repo_url="$2"
  local repo_name="${spec%%/*}"
  local chart_ver="${spec##*:}"
  local chart_name="${spec#*/}"
  chart_name="${chart_name%%:*}"

  log "Adding repo $repo_name ($repo_url)..."
  helm repo add "$repo_name" "$repo_url" --force-update 2>/dev/null || \
    helm repo add "$repo_name" "$repo_url"
  retry helm repo update

  local out="${CHARTS_DIR}/${chart_name}-${chart_ver}.tgz"
  if [[ -f "$out" ]]; then
    log "Already exists: $out"
    return 0
  fi

  log "Pulling ${repo_name}/${chart_name} ${chart_ver}..."
  retry helm pull "${repo_name}/${chart_name}" \
    --version "$chart_ver" \
    --destination "$CHARTS_DIR"
  log "Saved: $(ls -1 "${CHARTS_DIR}/${chart_name}"-*.tgz 2>/dev/null | tail -1)"
}

download_manifest() {
  local url="$1"
  local filename="$2"
  local out="${MANIFESTS_DIR}/${filename}"

  if [[ -f "$out" ]]; then
    log "Already exists: $out"
    return 0
  fi

  log "Downloading $url ..."
  retry curl -fsSL --connect-timeout 30 --max-time 300 "$url" -o "$out"
  log "Saved: $out"
}

log "=== Downloading Helm charts ==="
pull_chart "$CHART_INGRESS_NGINX" "$REPO_INGRESS_NGINX"
pull_chart "$CHART_CERT_MANAGER" "$REPO_JETSTACK"
pull_chart "$CHART_STRIMZI" "$REPO_STRIMZI"
pull_chart "$CHART_CNPG" "$REPO_CNPG"
pull_chart "$CHART_PROMETHEUS" "$REPO_PROMETHEUS"

log "=== Downloading K8s manifests ==="
download_manifest "$MANIFEST_LOCAL_PATH" "local-path-storage.yaml"
download_manifest "$MANIFEST_CLICKHOUSE_OP" "clickhouse-operator-install-bundle.yaml"

# Platform chart dependencies (postgresql, redis from bitnami)
log "=== Downloading platform chart dependencies ==="
helm repo add bitnami https://charts.bitnami.com/bitnami 2>/dev/null || true
retry helm repo update bitnami
cd "$REPO_ROOT/helm/platform"
retry helm dependency update
cd "$REPO_ROOT"

log ""
log "=== Done ==="
log "Vendor dir: $VENDOR_DIR"
ls -lh "$CHARTS_DIR/"
ls -lh "$MANIFESTS_DIR/"
log ""
log "Скопируйте на node1 (если скачивали локально):"
log "  rsync -avz deploy/vendor/ user@192.168.31.195:/home/user/dev/cloud_dwh/deploy/vendor/"
