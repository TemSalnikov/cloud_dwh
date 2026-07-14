#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: ЛОКАЛЬНАЯ (VPN → node1 registry + kubectl)
# ЗАЧЕМ:  собрать platform-api/ui, push в registry, helm upgrade
# ЗАПУСК: bash scripts/deploy-platform-update.sh
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
KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export KUBECONFIG

log() { echo "[deploy-platform] $*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }

need docker
need helm
need kubectl

log "Building images..."
docker build -t "cloud-dwh/platform-api:${TAG}" "$REPO_ROOT/platform-api"
docker build -t "cloud-dwh/platform-ui:${TAG}" "$REPO_ROOT/platform-ui"

log "Pushing to ${REGISTRY}..."
docker tag "cloud-dwh/platform-api:${TAG}" "${REGISTRY}/platform-api:${TAG}"
docker tag "cloud-dwh/platform-ui:${TAG}" "${REGISTRY}/platform-ui:${TAG}"
docker push "${REGISTRY}/platform-api:${TAG}"
docker push "${REGISTRY}/platform-ui:${TAG}"

log "Helm upgrade platform..."
helm upgrade platform "$REPO_ROOT/helm/platform" -n platform \
  --reuse-values \
  --set platformApi.image.repository="${REGISTRY}/platform-api" \
  --set platformApi.image.tag="${TAG}" \
  --set platformApi.image.pullPolicy=IfNotPresent \
  --set platformUi.image.repository="${REGISTRY}/platform-ui" \
  --set platformUi.image.tag="${TAG}" \
  --set platformUi.image.pullPolicy=IfNotPresent

log "Waiting for rollout..."
kubectl rollout status deployment/platform-api -n platform --timeout=180s
kubectl rollout status deployment/platform-ui -n platform --timeout=120s

log "Verify:"
kubectl exec -n platform deploy/platform-api -- grep -n "4.0.0\|KafkaNodePool" /app/app/services/provisioner.py | head -3
kubectl exec -n platform deploy/platform-ui -- grep -c deleteStack /usr/share/nginx/html/index.html

log "Done. Удалите старый стек в UI и создайте заново."
