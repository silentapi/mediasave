#!/usr/bin/env bash
# Deploy to the droplet: rsync the repo, bump the service-worker cache version, docker compose up -d --build.
# Usage: scripts/deploy.sh user@host [/remote/path]   (defaults: DEPLOY_HOST env, /opt/media-saver)
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${1:-${DEPLOY_HOST:-}}"
DEST="${2:-${DEPLOY_PATH:-/opt/media-saver}}"
[ -n "$HOST" ] || { echo "usage: $0 user@host [/remote/path]" >&2; exit 2; }

STAMP="ms-$(date -u +%Y%m%d%H%M%S)-$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
# Bump the SW version in a temp copy so the shell cache busts on every deploy.
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rsync -a --delete --exclude '.git' --exclude 'server/.env' --exclude '__pycache__' --exclude '.pytest_cache' ./ "$TMP/"
sed -i "s/^const VERSION = .*/const VERSION = '${STAMP}';/" "$TMP/server/static/sw.js"

rsync -az --delete --exclude 'server/.env' --exclude 'cookies.txt' "$TMP/" "$HOST:$DEST/"
ssh "$HOST" "cd '$DEST/server' && test -f .env || { echo 'server/.env missing on host — copy .env.example and fill it in' >&2; exit 1; }"
ssh "$HOST" "cd '$DEST/server' && docker compose up -d --build && docker compose ps"
echo "deployed $STAMP"
