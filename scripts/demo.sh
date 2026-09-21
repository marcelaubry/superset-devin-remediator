#!/usr/bin/env bash
# Docker-only demonstration of the remediator against the fake Devin/GitHub/Slack/probe
# adapters. Requires Docker (with Compose v2) and Git; nothing else is installed on the
# host and no live service is ever contacted.
#
#   ./scripts/demo.sh up      # create .env + local secrets, build and start the stack
#   ./scripts/demo.sh run     # drive the representative scenarios through the real endpoints
#   ./scripts/demo.sh token   # print the dashboard token
#   ./scripts/demo.sh down    # stop the stack (data kept)
#   ./scripts/demo.sh reset   # stop and DELETE the local demo database and volumes
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

compose() { docker compose "$@"; }

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  else
    docker run --rm python:3.11-slim python -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

# Replace a still-placeholder value in .env with a locally generated one. Never overwrites
# a value the operator already set.
replace_placeholder() {
  local key="$1" placeholder="$2" value tmp
  grep -q "^${key}=${placeholder}$" .env || return 0
  value="$(random_hex)"
  tmp="$(mktemp)"
  while IFS= read -r line; do
    if [ "$line" = "${key}=${placeholder}" ]; then printf '%s=%s\n' "$key" "$value"; else printf '%s\n' "$line"; fi
  done < .env > "$tmp"
  mv "$tmp" .env
  chmod 600 .env
}

setup() {
  if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "created .env from .env.example (fake mode: no live Devin, GitHub or Slack calls)"
  fi
  replace_placeholder GITHUB_WEBHOOK_SECRET change-me
  replace_placeholder OPERATOR_TOKEN change-me
  replace_placeholder SLACK_SIGNING_SECRET change-me-slack-signing

  mkdir -p docker/secrets
  if [ ! -s docker/secrets/verifier_hmac_key ]; then
    random_hex > docker/secrets/verifier_hmac_key
    # The verifier container runs as an unprivileged non-root uid and bind-mounts this file.
    chmod 644 docker/secrets/verifier_hmac_key
    echo "generated docker/secrets/verifier_hmac_key (git-ignored)"
  fi
}

token() { grep '^OPERATOR_TOKEN=' .env | cut -d= -f2-; }

wait_for_api() {
  local attempt
  for attempt in $(seq 1 60); do
    if compose exec -T api python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "api did not become healthy; see: docker compose logs api" >&2
  return 1
}

case "${1:-}" in
  up)
    setup
    compose up --build -d
    wait_for_api
    echo
    echo "dashboard: http://localhost:8000   token: $(token)"
    echo "next:      ./scripts/demo.sh run"
    ;;
  run)
    setup
    compose run --rm --no-deps -e BASE_URL=http://api:8000 api python scripts/simulate.py --scenario demo
    echo
    echo "dashboard: http://localhost:8000   token: $(token)"
    ;;
  token)
    token
    ;;
  down)
    compose down
    ;;
  reset)
    echo "deleting the local demo database and verifier workspace volumes"
    compose down -v --remove-orphans
    ;;
  *)
    sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
