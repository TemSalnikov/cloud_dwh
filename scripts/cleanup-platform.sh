#!/usr/bin/env bash
# Полная очистка platform namespace перед переустановкой
# ЗАПУСК: sudo bash scripts/cleanup-platform.sh
set -euo pipefail

export PATH="/usr/local/bin:${HOME}/.local/bin:/snap/bin:${PATH}"
if [[ -f /etc/kubernetes/admin.conf ]]; then
  export KUBECONFIG=/etc/kubernetes/admin.conf
fi

log() { echo "[cleanup-platform] $*"; }

log "Uninstalling helm release platform..."
helm uninstall platform -n platform 2>/dev/null || true

log "Deleting leftover resources in platform namespace..."
kubectl delete all --all -n platform --timeout=60s 2>/dev/null || true
kubectl delete pvc --all -n platform --timeout=60s 2>/dev/null || true
kubectl delete secret -n platform -l owner=helm 2>/dev/null || true
kubectl delete secret platform-postgresql platform-redis -n platform 2>/dev/null || true
kubectl delete ingress --all -n platform 2>/dev/null || true

# Остатки от старого bitnami chart
for kind in statefulset deployment service pvc secret configmap; do
  kubectl delete "$kind" -n platform -l app.kubernetes.io/instance=platform --timeout=30s 2>/dev/null || true
done

log "Remaining resources:"
kubectl get all,pvc,secret -n platform 2>/dev/null || echo "(namespace empty or missing)"

log "Done. Run: sudo bash scripts/bootstrap.sh"
