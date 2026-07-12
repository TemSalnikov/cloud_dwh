#!/usr/bin/env bash
# Проверка полноты offline-пакета перед передачей на node1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENDOR_DIR="$REPO_ROOT/deploy/vendor"
CHARTS_DIR="$VENDOR_DIR/charts"
MANIFESTS_DIR="$VENDOR_DIR/manifests"
PLATFORM_CHARTS="$REPO_ROOT/helm/platform/charts"
VENDOR_PLATFORM="$VENDOR_DIR/platform-charts"

log() { echo "[verify-offline] $*"; }
ok()  { echo "[verify-offline] OK  $*"; }
fail(){ echo "[verify-offline] FAIL $*" >&2; ERR=1; }

ERR=0

check_glob() {
  local label="$1" dir="$2" pattern="$3"
  local f
  f=$(ls -1 "$dir"/$pattern 2>/dev/null | head -1)
  if [[ -n "$f" && -f "$f" ]]; then
    ok "$label → $(basename "$f")"
  else
    fail "$label — не найден: $dir/$pattern"
  fi
}

log "=== Helm charts (bootstrap) ==="
check_glob "ingress-nginx"         "$CHARTS_DIR" "ingress-nginx*.tgz"
check_glob "cert-manager"          "$CHARTS_DIR" "cert-manager*.tgz"
check_glob "strimzi-kafka-operator" "$CHARTS_DIR" "strimzi-kafka-operator*.tgz"
check_glob "cloudnative-pg"        "$CHARTS_DIR" "cloudnative-pg*.tgz"
check_glob "kube-prometheus-stack" "$CHARTS_DIR" "kube-prometheus-stack*.tgz"

log "=== Platform chart dependencies ==="
if ls "$PLATFORM_CHARTS"/*.tgz &>/dev/null; then
  check_glob "postgresql" "$PLATFORM_CHARTS" "postgresql*.tgz"
  check_glob "redis"      "$PLATFORM_CHARTS" "redis*.tgz"
elif ls "$VENDOR_PLATFORM"/*.tgz &>/dev/null; then
  check_glob "postgresql" "$VENDOR_PLATFORM" "postgresql*.tgz"
  check_glob "redis"      "$VENDOR_PLATFORM" "redis*.tgz"
else
  fail "platform dependencies — нет charts в helm/platform/charts/ и deploy/vendor/platform-charts/"
fi

log "=== K8s manifests ==="
for f in local-path-storage.yaml clickhouse-operator-install-bundle.yaml; do
  if [[ -f "$MANIFESTS_DIR/$f" ]]; then
    ok "manifest → $f"
  else
    fail "manifest — не найден: $MANIFESTS_DIR/$f"
  fi
done

log "=== Размер пакета ==="
du -sh "$VENDOR_DIR" 2>/dev/null || true
du -sh "$PLATFORM_CHARTS" 2>/dev/null || du -sh "$VENDOR_PLATFORM" 2>/dev/null || true

if [[ $ERR -ne 0 ]]; then
  echo ""
  log "Пакет неполный. На локальной машине с VPN:"
  log "  bash scripts/download-vendor.sh"
  exit 1
fi

echo ""
ok "Offline-пакет готов к передаче на node1"
log "  bash scripts/sync-offline-bundle.sh"
