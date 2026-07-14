#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1 (192.168.31.195)
# ЗАЧЕМ:  установить kubectl, helm, docker, registry на сервер
#         чтобы node1 работал автономно без локальной машины
# ЗАПУСК: ssh user@192.168.31.195
#         sudo bash /home/user/dev/cloud_dwh/scripts/setup-node1.sh
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

HELM_VERSION="${HELM_VERSION:-v3.16.4}"
KUBECTL_VERSION="${KUBECTL_VERSION:-v1.31.4}"
INSTALL_DIR="${INSTALL_DIR:-/usr/local/bin}"
REPO_DIR="${REPO_DIR:-/home/user/dev/cloud_dwh}"

log() { echo "[setup-node1] $*"; }

[[ $(id -u) -eq 0 ]] || { echo "Запустите с sudo"; exit 1; }

ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  ARCH=amd64 ;;
  aarch64) ARCH=arm64 ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

log "Installing kubectl ${KUBECTL_VERSION} → ${INSTALL_DIR}/kubectl"
curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
  -o "${INSTALL_DIR}/kubectl"
chmod +x "${INSTALL_DIR}/kubectl"

log "Installing helm ${HELM_VERSION} → ${INSTALL_DIR}/helm"
curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" \
  -o /tmp/helm.tar.gz
tar -xzf /tmp/helm.tar.gz -C /tmp "linux-${ARCH}/helm"
mv "/tmp/linux-${ARCH}/helm" "${INSTALL_DIR}/helm"
chmod +x "${INSTALL_DIR}/helm"
rm -rf /tmp/helm.tar.gz "/tmp/linux-${ARCH}"

# kubeconfig для root и обычного пользователя
if [[ -f /etc/kubernetes/admin.conf ]]; then
  log "Configuring kubeconfig from /etc/kubernetes/admin.conf"
  mkdir -p /root/.kube
  cp /etc/kubernetes/admin.conf /root/.kube/config
  chown root:root /root/.kube/config

  # Для пользователя user (или того, кто запустил sudo)
  DEPLOY_USER="${SUDO_USER:-${SERVER_USER:-user}}"
  if id "$DEPLOY_USER" &>/dev/null; then
    USER_HOME=$(eval echo "~$DEPLOY_USER")
    mkdir -p "$USER_HOME/.kube"
    cp /etc/kubernetes/admin.conf "$USER_HOME/.kube/config"
    chown -R "$DEPLOY_USER:$DEPLOY_USER" "$USER_HOME/.kube"
    log "Kubeconfig copied to $USER_HOME/.kube/config (user: $DEPLOY_USER)"
  fi
else
  log "WARN: /etc/kubernetes/admin.conf not found — configure kubeconfig manually"
fi

# Docker (для сборки образов platform-api/ui)
if ! command -v docker &>/dev/null; then
  log "Installing docker..."
  apt-get update -qq
  apt-get install -y docker.io
  systemctl enable --now docker
  if id "${DEPLOY_USER}" &>/dev/null; then
    usermod -aG docker "${DEPLOY_USER}"
    log "User ${DEPLOY_USER} added to docker group (re-login required)"
  fi
fi

# Docker должен пушить в HTTP registry без TLS
REGISTRY="${REGISTRY:-${SERVER_IP}:5000}"
mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]] || ! grep -q "$REGISTRY" /etc/docker/daemon.json 2>/dev/null; then
  cat > /etc/docker/daemon.json <<EOF
{
  "insecure-registries": ["${REGISTRY}"]
}
EOF
  systemctl restart docker
fi

# Локальный registry (образы без внешнего registry)
if ! docker ps --format '{{.Names}}' | grep -q '^registry$'; then
  log "Starting local Docker registry on :5000"
  docker run -d -p 5000:5000 --restart=always --name registry registry:2
fi

# Директория проекта
mkdir -p "$(dirname "$REPO_DIR")"
if [[ ! -d "$REPO_DIR" ]]; then
  log "Project dir $REPO_DIR — скопируйте cloud_dwh туда (git clone или rsync)"
fi

log ""
log "=== Установлено ==="
kubectl version --client --short 2>/dev/null || kubectl version --client
helm version --short
docker --version 2>/dev/null || true
log ""
log "Следующие шаги (на node1):"
log "  1. Скопировать проект:  rsync/scp → $REPO_DIR"
log "  2. cd $REPO_DIR && sudo bash scripts/build-images.sh"
log "  3. cd $REPO_DIR && sudo bash scripts/bootstrap.sh"
log ""
log "Проверка: kubectl get nodes"
