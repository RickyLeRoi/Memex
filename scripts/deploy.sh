#!/usr/bin/env bash
# scripts/deploy.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-/docker/conf/compose.yaml}"
SERVICE="${SERVICE:-memex}"

cd "$REPO_DIR"
git pull --ff-only

docker compose -f "$COMPOSE_FILE" up -d --build "$SERVICE"
docker compose -f "$COMPOSE_FILE" logs --tail 20 "$SERVICE"
