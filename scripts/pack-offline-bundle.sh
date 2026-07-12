#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: ЛОКАЛЬНАЯ (с VPN / интернетом)
# ЗАЧЕМ:  скачать ВСЁ для offline bootstrap и проверить пакет
# ЗАПУСК: bash scripts/pack-offline-bundle.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log() { echo "[pack-offline] $*"; }

log "Шаг 1/3: скачивание charts, manifests, platform dependencies..."
bash "$SCRIPT_DIR/download-vendor.sh"

log "Шаг 2/3: копирование platform charts в vendor..."
VENDOR_PLATFORM="$REPO_ROOT/deploy/vendor/platform-charts"
mkdir -p "$VENDOR_PLATFORM"
if ls "$REPO_ROOT/helm/platform/charts/"*.tgz &>/dev/null; then
  cp -a "$REPO_ROOT/helm/platform/charts/"*.tgz "$VENDOR_PLATFORM/"
fi

log "Шаг 3/3: проверка пакета..."
bash "$SCRIPT_DIR/verify-offline-bundle.sh"

log ""
log "=== Готово ==="
log "Передать на node1:"
log "  bash scripts/sync-offline-bundle.sh"
