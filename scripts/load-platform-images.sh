#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1
# ЗАЧЕМ:  загрузить образы platform в containerd (для kubelet)
# ЗАПУСК: sudo bash scripts/load-platform-images.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TAR="$REPO_ROOT/deploy/vendor/images/platform-images.tar"
UPDATE_TAR="$REPO_ROOT/deploy/vendor/images/platform-api-update.tar"

log() { echo "[load-images] $*"; }

[[ $(id -u) -eq 0 ]] || { echo "Нужен sudo"; exit 1; }

# Prefer full bundle; fall back to api-only update
LOAD_FILE=""
if [[ -f "$TAR" ]]; then
  LOAD_FILE="$TAR"
elif [[ -f "$UPDATE_TAR" ]]; then
  LOAD_FILE="$UPDATE_TAR"
  log "Using update tar: $UPDATE_TAR"
else
  echo "Нет $TAR и $UPDATE_TAR — сначала sync-platform-images.sh"
  exit 1
fi

NS=k8s.io

if command -v ctr >/dev/null 2>&1; then
  log "Importing into containerd (ctr -n $NS)..."
  ctr -n "$NS" images import "$LOAD_FILE"
elif command -v nerdctl >/dev/null 2>&1; then
  log "Importing via nerdctl..."
  nerdctl -n k8s.io load -i "$LOAD_FILE"
elif command -v docker >/dev/null 2>&1; then
  log "Importing via docker..."
  docker load -i "$LOAD_FILE"
  if command -v ctr >/dev/null 2>&1; then
    ctr -n "$NS" images import "$LOAD_FILE"
  fi
else
  echo "Need ctr or docker"; exit 1
fi

log "Images:"
ctr -n "$NS" images ls 2>/dev/null | grep -E 'platform-api|platform-ui|postgres|redis' || true

log "Restart platform pods to pick up new images..."
kubectl --kubeconfig=/etc/kubernetes/admin.conf rollout restart deployment/platform-api deployment/platform-ui -n platform 2>/dev/null || true
kubectl --kubeconfig=/etc/kubernetes/admin.conf rollout status deployment/platform-api -n platform --timeout=120s 2>/dev/null || true
kubectl --kubeconfig=/etc/kubernetes/admin.conf rollout status deployment/platform-ui -n platform --timeout=60s 2>/dev/null || true

log "Done."
