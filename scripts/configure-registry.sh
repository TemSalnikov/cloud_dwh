#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1 (192.168.31.195)
# ЗАЧЕМ:  настроить containerd для локального registry :5000
# ЗАПУСК: sudo bash /opt/cloud_dwh/scripts/configure-registry.sh
# КОГДА:  если pods в ImagePullBackOff
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

if [[ -f /opt/cloud_dwh/deploy/server.env ]]; then
  # shellcheck source=/dev/null
  source /opt/cloud_dwh/deploy/server.env
fi

SERVER_IP="${SERVER_IP:-192.168.31.195}"
REGISTRY="${REGISTRY:-${SERVER_IP}:5000}"

CERTS_DIR="/etc/containerd/certs.d/${REGISTRY}"
mkdir -p "$CERTS_DIR"

cat > "${CERTS_DIR}/hosts.toml" <<EOF
server = "http://${REGISTRY}"

[host."http://${REGISTRY}"]
  capabilities = ["pull", "resolve", "push"]
  skip_verify = true
EOF

log() { echo "[registry-config] $*"; }

# Для kubeadm/containerd также может потребоваться mirrors в config.toml
if [[ -f /etc/containerd/config.toml ]]; then
  if ! grep -q "registry.mirrors" /etc/containerd/config.toml 2>/dev/null; then
    log "Add to /etc/containerd/config.toml under [plugins.\"io.containerd.grpc.v1.cri\".registry]:"
    log '  [plugins."io.containerd.grpc.v1.cri".registry.mirrors."'"${REGISTRY}"'"]'
    log '    endpoint = ["http://'"${REGISTRY}"'"]'
  fi
  systemctl restart containerd
fi

log "Configured ${CERTS_DIR}/hosts.toml"
log "Restart kubelet if images still fail: systemctl restart kubelet"
