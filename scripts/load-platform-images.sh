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

log() { echo "[load-images] $*"; }

[[ $(id -u) -eq 0 ]] || { echo "Нужен sudo"; exit 1; }
[[ -f "$TAR" ]] || { echo "Нет $TAR — сначала sync-platform-images.sh с локальной машины"; exit 1; }

# containerd namespace для Kubernetes
NS=k8s.io

if command -v ctr >/dev/null 2>&1; then
  log "Importing into containerd (ctr -n $NS)..."
  ctr -n "$NS" images import "$TAR"
elif command -v nerdctl >/dev/null 2>&1; then
  log "Importing via nerdctl..."
  nerdctl -n k8s.io load -i "$TAR"
elif command -v docker >/dev/null 2>&1; then
  log "Importing via docker + ctr..."
  docker load -i "$TAR"
  # Re-export and import to containerd if both exist
  if command -v ctr >/dev/null 2>&1; then
    ctr -n "$NS" images import "$TAR"
  else
    log "WARN: docker load done, but kubelet uses containerd — install ctr or use:"
    log "  ctr -n k8s.io images import $TAR"
  fi
else
  echo "Need ctr or docker"; exit 1
fi

log "Images in containerd:"
ctr -n "$NS" images ls 2>/dev/null | grep -E 'platform-api|platform-ui|postgres|redis' || true

log "Restart pods to pick up local images..."
kubectl --kubeconfig=/etc/kubernetes/admin.conf delete pods -n platform --all 2>/dev/null || true

log "Done. Check: kubectl get pods -n platform -w"
