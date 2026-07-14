#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: ЛОКАЛЬНАЯ (kubectl → node1)
# ЗАЧЕМ:  загрузить platform-api/ui в containerd без SSH/registry TLS
# ЗАПУСК: bash scripts/import-platform-images.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_DIR=/tmp/cloud-dwh-import
TAR="${TAR:-$REPO_ROOT/deploy/vendor/images/platform-api-update.tar}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

log() { echo "[import-images] $*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }

need kubectl
need docker
[[ -f "$TAR" ]] || { echo "Missing tar: $TAR"; exit 1; }

log "Importing $TAR into node1 containerd..."
kubectl delete pod image-import -n platform --ignore-not-found --wait=true 2>/dev/null || true

kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: image-import
  namespace: platform
spec:
  restartPolicy: Never
  nodeName: node1
  hostPID: true
  hostNetwork: true
  containers:
  - name: import
    image: ubuntu:22.04
    command: ["sleep", "900"]
    securityContext:
      privileged: true
    volumeMounts:
    - name: host-tmp
      mountPath: /host-tmp
  volumes:
  - name: host-tmp
    hostPath:
      path: ${HOST_DIR}
      type: DirectoryOrCreate
EOF

kubectl wait --for=condition=Ready pod/image-import -n platform --timeout=90s
kubectl cp "$TAR" platform/image-import:/host-tmp/$(basename "$TAR")
kubectl exec -n platform image-import -- bash -c \
  'DEBIAN_FRONTEND=noninteractive apt-get update -qq && apt-get install -y -qq util-linux >/dev/null && nsenter -t 1 -m -u -i -n -- ctr -n k8s.io images import '"${HOST_DIR}/$(basename "$TAR")"
kubectl delete pod image-import -n platform --wait=true

log "Restart platform deployments..."
kubectl rollout restart deployment/platform-api deployment/platform-ui -n platform
kubectl rollout status deployment/platform-api -n platform --timeout=180s
kubectl rollout status deployment/platform-ui -n platform --timeout=120s
log "Done."
