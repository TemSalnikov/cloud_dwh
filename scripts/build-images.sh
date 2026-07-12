#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1 (192.168.31.195)
# ЗАЧЕМ:  собрать образы platform-api/ui и загрузить в локальный registry
# ЗАПУСК: sudo bash /home/user/dev/cloud_dwh/scripts/build-images.sh
# ТРЕБУЕТ: setup-node1.sh (docker + registry :5000)
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
REGISTRY="${REGISTRY:-${SERVER_IP}:5000}"
TAG="${TAG:-0.1.0}"

log() { echo "[build-images] $*"; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }

need docker

log "Building platform-api..."
docker build -t "cloud-dwh/platform-api:${TAG}" "$REPO_ROOT/platform-api"
docker tag "cloud-dwh/platform-api:${TAG}" "${REGISTRY}/platform-api:${TAG}"
docker push "${REGISTRY}/platform-api:${TAG}"

log "Building platform-ui..."
docker build -t "cloud-dwh/platform-ui:${TAG}" "$REPO_ROOT/platform-ui"
docker tag "cloud-dwh/platform-ui:${TAG}" "${REGISTRY}/platform-ui:${TAG}"
docker push "${REGISTRY}/platform-ui:${TAG}"

log "Updating helm values for local registry..."
VALUES="$REPO_ROOT/helm/platform/values.yaml"
# Patch image references (idempotent via grep check)
if ! grep -q "${REGISTRY}/platform-api" "$VALUES"; then
  sed -i "s|repository: cloud-dwh/platform-api|repository: ${REGISTRY}/platform-api|" "$VALUES"
  sed -i "s|repository: cloud-dwh/platform-ui|repository: ${REGISTRY}/platform-ui|" "$VALUES"
fi

log "Done. Images at ${REGISTRY}/platform-api:${TAG} and platform-ui:${TAG}"
log "Configure containerd to trust insecure registry if needed:"
log "  /etc/containerd/certs.d/${SERVER_IP}:5000/hosts.toml"
