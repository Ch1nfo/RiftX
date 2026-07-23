#!/usr/bin/env bash
set -euo pipefail

readonly IMAGE="${RIFTX_SANDBOX_IMAGE:-riftx/sandbox:ci}"
readonly PROJECT="riftx-demo"
readonly COMPOSE_FILE="deploy/demo/compose.yaml"
readonly TARGET_NETWORK="${PROJECT}_target"

cleanup() {
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" down --volumes --remove-orphans
}
trap cleanup EXIT

docker compose -p "$PROJECT" -f "$COMPOSE_FILE" up -d

test "$(docker run --rm --entrypoint id "$IMAGE" -u)" = "10001"
docker run --rm --entrypoint sh "$IMAGE" -c \
  'command -v codex && command -v nmap && command -v httpx && command -v nuclei && command -v ffuf'

wait_for_target() {
  local url="$1"
  for _ in $(seq 1 60); do
    if docker run --rm --network "$TARGET_NETWORK" --entrypoint httpx "$IMAGE" \
      -silent -status-code -u "$url" | grep -Eq '\[(200|301|302)\]'; then
      return 0
    fi
    sleep 2
  done
  echo "target did not become ready: $url" >&2
  return 1
}

wait_for_target "http://juice-shop:3000/"
wait_for_target "http://dvwa/"

docker run --rm --network "$TARGET_NETWORK" --entrypoint nuclei "$IMAGE" \
  -silent -jsonl -duc -t /opt/riftx/nuclei-templates -u http://dvwa/ -tags dvwa \
  | grep -q 'riftx-dvwa-login'
