#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1 (и любая машина, которая делает docker push)
# ЗАЧЕМ:  разрешить HTTP registry :5000 для Docker + containerd
# ЗАПУСК: sudo bash scripts/configure-registry.sh
# КОГДА:  docker push → "HTTP response to HTTPS client"
#         или pods в ImagePullBackOff
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

log() { echo "[registry-config] $*"; }

# ── 1) Docker: insecure-registries (нужно для docker push) ─────
DOCKER_DAEMON="/etc/docker/daemon.json"
mkdir -p /etc/docker

if [[ -f "$DOCKER_DAEMON" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$DOCKER_DAEMON" "$REGISTRY" <<'PY'
import json, sys
path, reg = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {}
regs = list(data.get("insecure-registries") or [])
if reg not in regs:
    regs.append(reg)
data["insecure-registries"] = regs
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"updated {path}: insecure-registries={regs}")
PY
  else
    log "python3 missing — write daemon.json manually with insecure-registries: [\"${REGISTRY}\"]"
  fi
else
  cat > "$DOCKER_DAEMON" <<EOF
{
  "insecure-registries": ["${REGISTRY}"]
}
EOF
  log "Created ${DOCKER_DAEMON}"
fi

if systemctl is-active --quiet docker 2>/dev/null; then
  log "Restarting docker..."
  systemctl restart docker
  # registry container must survive restart (restart=always)
  sleep 2
  if ! docker ps --format '{{.Names}}' | grep -q '^registry$'; then
    log "Starting local registry on :5000"
    docker run -d -p 5000:5000 --restart=always --name registry registry:2 || true
  fi
fi

# ── 2) containerd: HTTP pull для kubelet ───────────────────────
CERTS_DIR="/etc/containerd/certs.d/${REGISTRY}"
mkdir -p "$CERTS_DIR"

cat > "${CERTS_DIR}/hosts.toml" <<EOF
server = "http://${REGISTRY}"

[host."http://${REGISTRY}"]
  capabilities = ["pull", "resolve", "push"]
  skip_verify = true
EOF

if [[ -f /etc/containerd/config.toml ]]; then
  if ! grep -q "certs.d" /etc/containerd/config.toml 2>/dev/null; then
    log "Ensure containerd uses certs.d (kubeadm usually has this)."
  fi
  systemctl restart containerd 2>/dev/null || true
fi

log "OK: Docker + containerd allow http://${REGISTRY}"
log "Retry: sudo bash scripts/build-images.sh"
