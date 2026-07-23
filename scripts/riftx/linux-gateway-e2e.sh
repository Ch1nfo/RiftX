#!/usr/bin/env bash
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly WORK="${RIFTX_E2E_WORK:-/tmp/riftx-e2e}"
readonly GATEWAY_BIN="${RIFTX_GATEWAY_BIN:-$ROOT/codex-rs/target/debug/riftx-gateway}"
readonly CLI_BIN="${RIFTX_CLI_BIN:-$ROOT/codex-rs/target/debug/riftx}"
readonly MANAGER_BIN="${RIFTX_MANAGER_BIN:-$WORK/sandbox-managerd}"
readonly OPERATOR_TOKEN="riftx-e2e-operator-token"
readonly MODEL_PORT=8766
readonly GATEWAY_PORT=8787

cleanup() {
  set +e
  sudo kill "${GATEWAY_PID:-}" "${MANAGER_PID:-}" 2>/dev/null
  kill "${MODEL_PID:-}" 2>/dev/null
  docker ps -aq --filter label=riftx.engagement | xargs -r docker rm -f
  docker compose -p riftx-demo -f "$ROOT/deploy/demo/compose.yaml" down --volumes --remove-orphans
  sudo rm -rf "$WORK"
}
trap cleanup EXIT

sudo rm -rf "$WORK"
mkdir -p "$WORK/codex-home"
docker compose -p riftx-demo -f "$ROOT/deploy/demo/compose.yaml" up -d
juice_ip="$(docker inspect -f '{{with index .NetworkSettings.Networks "riftx-demo_target"}}{{.IPAddress}}{{end}}' riftx-demo-juice-shop-1)"
test -n "$juice_ip"
for _ in $(seq 1 60); do
  if docker run --rm --network riftx-demo_target --entrypoint httpx riftx/sandbox:ci \
    -silent -status-code -u "http://${juice_ip}:3000" | grep -q '\[200\]'; then
    break
  fi
  sleep 2
done
docker run --rm --network riftx-demo_target --entrypoint httpx riftx/sandbox:ci \
  -silent -status-code -u "http://${juice_ip}:3000" | grep -q '\[200\]'

cat >"$WORK/codex-home/config.toml" <<EOF
model = "gpt-5.2"
model_provider = "riftx_mock"

[model_providers.riftx_mock]
name = "RiftX deterministic mock"
base_url = "http://127.0.0.1:${MODEL_PORT}/v1"
env_key = "RIFTX_MOCK_API_KEY"
wire_api = "responses"
EOF

cat >"$WORK/riftx.toml" <<EOF
[gateway]
listen = "127.0.0.1:${GATEWAY_PORT}"
operator_token_env = "RIFTX_OPERATOR_TOKEN"
state_db = "$WORK/state.sqlite"

[manager]
socket = "$WORK/managerd.sock"
request_timeout_ms = 10000

[sandbox]
image = "riftx/sandbox:ci"
cpu_limit = 2
memory_mib = 2048
pids_limit = 512

[policy]
allowed_tools = ["rt_nmap", "rt_httpx", "rt_nuclei", "rt_ffuf"]
denied_cidrs = []
denied_domains = []

[audit]
jsonl_path = "$WORK/audit.jsonl"
fsync = true

[artifacts]
root = "$WORK/artifacts"
max_bytes_per_engagement = 1073741824

[tool_profiles.recon]
allowed_tools = ["rt_nmap", "rt_httpx", "rt_nuclei", "rt_ffuf"]
approval = "high_risk"

[tool_profiles.recon.scope]
cidrs = ["0.0.0.0/0"]
domains = ["*"]
ports = []
EOF

python3 "$ROOT/scripts/riftx/mock-responses-http.py" \
  --port "$MODEL_PORT" --target "http://${juice_ip}:3000" &
MODEL_PID=$!

sudo "$MANAGER_BIN" \
  -socket "$WORK/managerd.sock" \
  -artifact-root "$WORK/artifacts" \
  -credential-root "$WORK/credentials" &
MANAGER_PID=$!

for _ in $(seq 1 50); do
  test -S "$WORK/managerd.sock" && break
  sleep 0.1
done
test -S "$WORK/managerd.sock"

sudo env \
  CODEX_HOME="$WORK/codex-home" \
  RIFTX_OPERATOR_TOKEN="$OPERATOR_TOKEN" \
  RIFTX_MOCK_API_KEY="deterministic-test-key" \
  "$GATEWAY_BIN" --config "$WORK/riftx.toml" &
GATEWAY_PID=$!

for _ in $(seq 1 100); do
  if curl -sS -o /dev/null -H "Authorization: Bearer $OPERATOR_TOKEN" \
    "http://127.0.0.1:${GATEWAY_PORT}/v1/engagements/missing" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

export RIFTX_GATEWAY_URL="http://127.0.0.1:${GATEWAY_PORT}"
export RIFTX_OPERATOR_TOKEN="$OPERATOR_TOKEN"
engagement="$($CLI_BIN create --name "Juice Shop CI" --cidr "${juice_ip}/32" --port 3000)"
engagement_id="$(jq -r .id <<<"$engagement")"
activation="$($CLI_BIN activate "$engagement_id")"
sandbox_id="$(jq -r .sandboxId <<<"$activation")"
docker network connect riftx-demo_target "riftx-${sandbox_id}"

$CLI_BIN turn "$engagement_id" --agent recon "Probe the authorized HTTP service."

for _ in $(seq 1 100); do
  report="$($CLI_BIN report "$engagement_id" --format json)"
  if jq -e '.assets | length > 0' <<<"$report" >/dev/null; then
    jq -e '.services | length > 0' <<<"$report" >/dev/null
    jq -e '.tasks[] | select(.status == "completed")' <<<"$report" >/dev/null
    grep -q '"event":"tool/completed"' "$WORK/audit.jsonl"
    exit 0
  fi
  sleep 0.2
done

echo "Gateway E2E did not persist reconnaissance state" >&2
exit 1
