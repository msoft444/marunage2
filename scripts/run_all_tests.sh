#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_CMD=(docker compose -f "$ROOT_DIR/docker-compose.test.yml")

cleanup() {
  "${COMPOSE_CMD[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}

trap cleanup EXIT

wait_for_mariadb() {
  local container_id health attempt

  for attempt in $(seq 1 60); do
    container_id="$("${COMPOSE_CMD[@]}" ps -q mariadb)"
    if [[ -n "$container_id" ]]; then
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
      if [[ "$health" == "healthy" ]]; then
        return 0
      fi
      if [[ "$health" == "exited" || "$health" == "dead" ]]; then
        break
      fi
    fi
    sleep 2
  done

  echo "mariadb test container did not become healthy" >&2
  "${COMPOSE_CMD[@]}" logs mariadb >&2 || true
  return 1
}

cd "$ROOT_DIR"
"${COMPOSE_CMD[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE_CMD[@]}" up -d mariadb
wait_for_mariadb
"${COMPOSE_CMD[@]}" build tests
"${COMPOSE_CMD[@]}" run --rm tests