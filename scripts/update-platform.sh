#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1
# ЗАЧЕМ:  одной командой пересобрать platform-api/ui и выкатить в K8s
# ЗАПУСК: sudo bash scripts/update-platform.sh
#
# Обходит HTTP/HTTPS проблемы registry: docker build → ctr import
# (образы сразу в containerd namespace k8s.io, pullPolicy: Never)
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
# Должен совпадать с helm/platform/values.yaml (сейчас 0.1.5)
TAG="${TAG:-0.1.5}"
KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
API_IMAGE="cloud-dwh/platform-api:${TAG}"
UI_IMAGE="cloud-dwh/platform-ui:${TAG}"

log() { echo "[update-platform] $*"; }

[[ $(id -u) -eq 0 ]] || { echo "Нужен sudo"; exit 1; }
command -v docker >/dev/null || { echo "Нужен docker"; exit 1; }
command -v ctr >/dev/null || { echo "Нужен ctr (containerd)"; exit 1; }
command -v kubectl >/dev/null || { echo "Нужен kubectl"; exit 1; }

# swap ломает kubelet — на всякий случай
swapoff -a 2>/dev/null || true

log "Building ${API_IMAGE}..."
docker build -t "${API_IMAGE}" -t "${REGISTRY}/platform-api:${TAG}" "$REPO_ROOT/platform-api"

log "Building ${UI_IMAGE}..."
docker build -t "${UI_IMAGE}" -t "${REGISTRY}/platform-ui:${TAG}" "$REPO_ROOT/platform-ui"

log "Importing into containerd (k8s.io)..."
docker save "${API_IMAGE}" | ctr -n k8s.io images import -
docker save "${UI_IMAGE}" | ctr -n k8s.io images import -

# На всякий случай — push в local registry (может упасть на TLS; не критично)
if docker push "${REGISTRY}/platform-api:${TAG}" 2>/dev/null; then
  docker push "${REGISTRY}/platform-ui:${TAG}" 2>/dev/null || true
  log "Also pushed to ${REGISTRY}"
else
  log "Registry push skipped (OK — images loaded into containerd)"
fi

log "Aligning Deployment images + IfNotPresent..."
kubectl --kubeconfig="$KUBECONFIG" set image deployment/platform-api -n platform \
  "api=${API_IMAGE}" 2>/dev/null || true
kubectl --kubeconfig="$KUBECONFIG" set image deployment/platform-ui -n platform \
  "ui=${UI_IMAGE}" 2>/dev/null || true

kubectl --kubeconfig="$KUBECONFIG" patch deployment platform-api -n platform \
  --type=json -p="[
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/image\",\"value\":\"${API_IMAGE}\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/imagePullPolicy\",\"value\":\"IfNotPresent\"}
  ]" 2>/dev/null || true

kubectl --kubeconfig="$KUBECONFIG" patch deployment platform-ui -n platform \
  --type=json -p="[
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/image\",\"value\":\"${UI_IMAGE}\"},
    {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/imagePullPolicy\",\"value\":\"IfNotPresent\"}
  ]" 2>/dev/null || true

# helm upgrade если chart/values менялись
if command -v helm >/dev/null 2>&1; then
  log "Helm upgrade (reuse-values + new image tags)..."
  helm upgrade platform "$REPO_ROOT/helm/platform" -n platform --reuse-values \
    --kubeconfig "$KUBECONFIG" \
    --set platformApi.image.repository=cloud-dwh/platform-api \
    --set platformApi.image.tag="${TAG}" \
    --set platformApi.image.pullPolicy=IfNotPresent \
    --set platformUi.image.repository=cloud-dwh/platform-ui \
    --set platformUi.image.tag="${TAG}" \
    --set platformUi.image.pullPolicy=IfNotPresent \
    || log "helm upgrade skipped/failed — continuing with rollout"
fi

log "Rolling out..."
kubectl --kubeconfig="$KUBECONFIG" rollout restart deployment/platform-api deployment/platform-ui -n platform
kubectl --kubeconfig="$KUBECONFIG" rollout status deployment/platform-api -n platform --timeout=180s
kubectl --kubeconfig="$KUBECONFIG" rollout status deployment/platform-ui -n platform --timeout=120s

log "Pods:"
kubectl --kubeconfig="$KUBECONFIG" get pods -n platform -l 'app in (platform-api,platform-ui)'

log "Done. UI: https://platform.${SERVER_IP}.nip.io"
log "Admin: admin@cloud-dwh.local / ChangeMeAdmin1!"
