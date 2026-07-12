#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: ЛОКАЛЬНАЯ
# ЗАЧЕМ:  передать platform-images.tar + helm на node1
# ЗАПУСК: bash scripts/sync-platform-images.sh
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
TAR="$REPO_ROOT/deploy/vendor/images/platform-images.tar"

log() { echo "[sync-images] $*"; }
[[ -f "$TAR" ]] || { echo "Нет $TAR — сначала: bash scripts/pack-platform-images.sh"; exit 1; }

log "Sync images + helm platform → ${SERVER_USER}@${SERVER_IP}"
ssh "${SERVER_USER}@${SERVER_IP}" "mkdir -p ${REPO_DIR}/deploy/vendor/images ${REPO_DIR}/helm/platform/templates ${REPO_DIR}/scripts"

rsync -avz --progress "$TAR" \
  "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/deploy/vendor/images/"

rsync -avz \
  "$REPO_ROOT/helm/platform/" \
  "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/helm/platform/"

rsync -avz \
  "$REPO_ROOT/scripts/load-platform-images.sh" \
  "$REPO_ROOT/scripts/bootstrap.sh" \
  "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/scripts/"

log "Done. На node1:"
log "  sudo bash ${REPO_DIR}/scripts/load-platform-images.sh"
log "  sudo bash ${REPO_DIR}/scripts/bootstrap.sh"
