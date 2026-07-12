#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: ЛОКАЛЬНАЯ (с VPN)
# ЗАЧЕМ:  передать offline-пакет на node1 (charts, manifests, platform deps)
# ЗАПУСК: bash scripts/sync-offline-bundle.sh
# ТРЕБУЕТ: bash scripts/pack-offline-bundle.sh (выполнен ранее)
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -f "$REPO_ROOT/deploy/server.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$REPO_ROOT/deploy/server.env"
  set +a
fi

SERVER_IP="${SERVER_IP:-192.168.31.195}"
SERVER_USER="${SERVER_USER:-user}"
REPO_DIR="${REPO_DIR:-/home/user/dev/cloud_dwh}"

log() { echo "[sync-offline] $*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }

need rsync
need ssh

log "Проверка offline-пакета на локальной машине..."
bash "$SCRIPT_DIR/verify-offline-bundle.sh"

log "Создание директорий на node1..."
ssh "${SERVER_USER}@${SERVER_IP}" "mkdir -p ${REPO_DIR}/deploy/vendor ${REPO_DIR}/helm/platform/charts"

log "1/4 deploy/vendor/ (charts + manifests)..."
rsync -avz --progress \
  "$REPO_ROOT/deploy/vendor/" \
  "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/deploy/vendor/"

log "2/4 helm/platform/charts/ (postgresql, redis)..."
if ls "$REPO_ROOT/helm/platform/charts/"*.tgz &>/dev/null; then
  rsync -avz --progress \
    "$REPO_ROOT/helm/platform/charts/" \
    "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/helm/platform/charts/"
elif ls "$REPO_ROOT/deploy/vendor/platform-charts/"*.tgz &>/dev/null; then
  rsync -avz --progress \
    "$REPO_ROOT/deploy/vendor/platform-charts/" \
    "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/helm/platform/charts/"
fi

log "3/4 helm/platform/Chart.lock..."
rsync -avz \
  "$REPO_ROOT/helm/platform/Chart.lock" \
  "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/helm/platform/Chart.lock"

log "4/4 scripts/ + deploy configs..."
rsync -avz \
  "$REPO_ROOT/scripts/bootstrap.sh" \
  "$REPO_ROOT/scripts/verify-offline-bundle.sh" \
  "$REPO_ROOT/scripts/download-vendor.sh" \
  "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/scripts/"

ssh "${SERVER_USER}@${SERVER_IP}" "mkdir -p ${REPO_DIR}/deploy/ingress-nginx"
rsync -avz \
  "$REPO_ROOT/deploy/ingress-nginx/" \
  "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/deploy/ingress-nginx/"
rsync -avz \
  "$REPO_ROOT/deploy/server.env" \
  "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/deploy/server.env"

log ""
log "=== Передача завершена ==="
log "На node1 выполните:"
log "  ssh ${SERVER_USER}@${SERVER_IP}"
log "  bash ${REPO_DIR}/scripts/verify-offline-bundle.sh"
log "  sudo bash ${REPO_DIR}/scripts/bootstrap.sh"
