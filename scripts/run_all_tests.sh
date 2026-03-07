#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_CMD=(docker compose -f "$ROOT_DIR/docker-compose.test.yml")

cleanup() {
  "${COMPOSE_CMD[@]}" down --volumes --remove-orphans
}

trap cleanup EXIT

cd "$ROOT_DIR"
"${COMPOSE_CMD[@]}" up --build --abort-on-container-exit --exit-code-from tests tests