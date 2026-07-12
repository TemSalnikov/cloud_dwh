#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: node1 (192.168.31.195) — или локальная с kubeconfig
# ЗАЧЕМ:  проверить доступность K8s-кластера перед bootstrap
# ЗАПУСК: bash /opt/cloud_dwh/scripts/check-cluster.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SERVER_IP="${SERVER_IP:-192.168.31.195}"
KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

export KUBECONFIG

log() { echo "[check] $*"; }
fail() { echo "[check] ERROR: $*" >&2; exit 1; }

export PATH="${HOME}/.local/bin:/snap/bin:${PATH}"

need() { command -v "$1" >/dev/null 2>&1 || fail "Установите $1: curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"; }

log "Ping $SERVER_IP..."
ping -c 1 -W 3 "$SERVER_IP" >/dev/null || fail "Сервер $SERVER_IP недоступен"

log "Kubeconfig: $KUBECONFIG"
[[ -f "$KUBECONFIG" ]] || fail "Kubeconfig не найден: $KUBECONFIG"

grep -q "$SERVER_IP" "$KUBECONFIG" || fail "Kubeconfig не указывает на $SERVER_IP"

need kubectl

log "API server..."
kubectl cluster-info

log "Nodes:"
kubectl get nodes -o wide

log "Allocatable resources:"
kubectl describe node | grep -A5 "Allocatable:"

log "Existing namespaces:"
kubectl get ns

log "Storage classes:"
kubectl get sc 2>/dev/null || echo "  (none)"

log "Cluster OK — можно запускать ./scripts/bootstrap.sh"
