#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force|-f) FORCE=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: ./setup.sh [--force]

Creates .env from .env.example and fills in generated secrets.
Existing non-empty values are kept unless --force is given.
EOF
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

if [ ! -f .env.example ]; then
  echo "Missing .env.example; run this from the repo root." >&2
  exit 1
fi

umask 077

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

gen_hex() {
  local bytes="${1:-32}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$bytes"
  else
    head -c "$bytes" /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

get_key() {
  sed -n "s/^$1=//p" .env | tail -n 1
}

set_key() {
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

ensure_secret() {
  local key="$1" bytes="$2" current
  current="$(get_key "$key")"
  if [ -n "$current" ] && [ "$FORCE" -eq 0 ]; then
    printf '%s' "$current"
    return
  fi
  gen_hex "$bytes"
}

TFG_API_KEY="$(ensure_secret TFG_API_KEY 32)"
BROWSER_PASSWORD="$(ensure_secret BROWSER_PASSWORD 16)"
CREWAI_API_KEY="$(ensure_secret CREWAI_API_KEY 32)"

set_key TFG_API_KEY "$TFG_API_KEY"
set_key BROWSER_PASSWORD "$BROWSER_PASSWORD"
set_key CREWAI_API_KEY "$CREWAI_API_KEY"

if [ -z "$(get_key PUID)" ]; then
  set_key PUID "$(id -u)"
fi
if [ -z "$(get_key PGID)" ]; then
  set_key PGID "$(id -g)"
fi

if [ -z "$(get_key TZ)" ]; then
  tz="UTC"
  if [ -f /etc/timezone ]; then
    tz="$(cat /etc/timezone)"
  fi
  if command -v timedatectl >/dev/null 2>&1; then
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || printf '%s' "$tz")"
  fi
  set_key TZ "$tz"
fi

chmod 600 .env

browser_user="$(get_key BROWSER_USER)"
[ -n "$browser_user" ] || browser_user="admin"
browser_port="$(get_key BROWSER_GUI_HOST_PORT)"
[ -n "$browser_port" ] || browser_port="6081"

echo
echo ".env is ready (mode 600, gitignored)."
echo
echo "Browser desktop: https://localhost:${browser_port}"
echo "  user:     ${browser_user}"
echo "  password: ${BROWSER_PASSWORD}"
echo
echo "Gateway bearer: ${TFG_API_KEY}"
echo "CrewAI bearer:  ${CREWAI_API_KEY}"
echo
echo "Next: docker compose up -d --build"
if [ "$FORCE" -eq 1 ]; then
  echo
  echo "Secrets were regenerated. Recreate containers for them to take effect:"
  echo "  docker compose up -d"
fi
