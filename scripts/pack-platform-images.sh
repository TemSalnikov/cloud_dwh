#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# МАШИНА: ЛОКАЛЬНАЯ (с VPN / интернетом)
# ЗАЧЕМ:  собрать platform-api/ui + скачать postgres/redis → tar
# ЗАПУСК: bash scripts/pack-platform-images.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGES_DIR="$REPO_ROOT/deploy/vendor/images"
TAG="${TAG:-0.1.0}"

log() { echo "[pack-images] $*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }

need docker
mkdir -p "$IMAGES_DIR"

log "Building platform-api..."
docker build -t "cloud-dwh/platform-api:${TAG}" "$REPO_ROOT/platform-api"

log "Building platform-ui..."
docker build -t "cloud-dwh/platform-ui:${TAG}" "$REPO_ROOT/platform-ui"

log "Pulling postgres:16-alpine and redis:7-alpine..."
docker pull postgres:16-alpine
docker pull redis:7-alpine

log "Saving images to $IMAGES_DIR ..."
docker save \
  "cloud-dwh/platform-api:${TAG}" \
  "cloud-dwh/platform-ui:${TAG}" \
  postgres:16-alpine \
  redis:7-alpine \
  -o "$IMAGES_DIR/platform-images.tar"

ls -lh "$IMAGES_DIR/platform-images.tar"
log ""
log "Далее передать на node1:"
log "  bash scripts/sync-platform-images.sh"
log "  # на node1: sudo bash scripts/load-platform-images.sh"
