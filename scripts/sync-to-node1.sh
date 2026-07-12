#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: локальная (ваш ПК)
# ЗАЧЕМ:  однократно скопировать проект на node1
# ЗАПУСК: ./scripts/sync-to-node1.sh
# ТРЕБУЕТ: ssh-доступ к user@192.168.31.195
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

log() { echo "[sync-to-node1] $*"; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }
need rsync
need ssh

log "Syncing $REPO_ROOT → ${SERVER_USER}@${SERVER_IP}:${REPO_DIR}"

ssh "${SERVER_USER}@${SERVER_IP}" "mkdir -p ${REPO_DIR}"

rsync -avz --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '.venv' \
  "$REPO_ROOT/" "${SERVER_USER}@${SERVER_IP}:${REPO_DIR}/"

log "Done. Now on node1 run:"
log "  ssh ${SERVER_USER}@${SERVER_IP}"
log "  sudo bash ${REPO_DIR}/scripts/setup-node1.sh    # один раз"
log "  sudo bash ${REPO_DIR}/scripts/build-images.sh"
log "  sudo bash ${REPO_DIR}/scripts/bootstrap.sh"
