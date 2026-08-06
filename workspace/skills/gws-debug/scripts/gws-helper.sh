#!/usr/bin/env bash
set -euo pipefail

GWS_CONFIG_DIR="${GOOGLE_WORKSPACE_CLI_CONFIG_DIR:-$HOME/.config/gws}"
USER_TOKEN_CACHE="$GWS_CONFIG_DIR/token_cache.json"
SERVICE_ACCOUNT_TOKEN_CACHE="$GWS_CONFIG_DIR/sa_token_cache.json"
COMBINED_SERVICES="${GWS_SERVICES:-gmail,chat,drive,calendar,docs,sheets,tasks}"

usage() {
  cat <<'USAGE'
gws-helper.sh — repeatable Google Workspace CLI diagnostics

Usage:
  gws-helper.sh status
  gws-helper.sh verify [gmail] [drive] [calendar] [chat]
  gws-helper.sh auth-combined
  gws-helper.sh heal-cache

Notes:
  - status and verify are read-only.
  - auth-combined starts OAuth and prints remote callback tunnel instructions.
  - heal-cache backs up and removes access-token caches; get user confirmation first.
USAGE
}

require_command() {
  command -v "$1" >/dev/null || {
    echo "Missing required command: $1" >&2
    exit 127
  }
}

status() {
  gws auth status
}

verify() {
  local services=("$@")
  if [[ ${#services[@]} -eq 0 ]]; then
    services=(gmail drive calendar)
  fi

  local service
  for service in "${services[@]}"; do
    echo "== $service ==" >&2
    case "$service" in
      gmail) gws gmail users getProfile --params '{"userId":"me"}' --format json ;;
      drive) gws drive files list --params '{"pageSize":1,"q":"trashed=false","fields":"files(id,name)"}' --format json ;;
      calendar) gws calendar calendarList list --params '{"maxResults":1}' --format json ;;
      chat) gws chat spaces list --params '{"pageSize":1}' --format json ;;
      *) echo "Unknown service: $service" >&2; exit 2 ;;
    esac
  done
}

extract_callback_port() {
  local auth_url="$1"
  python3 - "$auth_url" <<'PY'
import sys
from urllib.parse import parse_qs, unquote, urlparse

auth_url = sys.argv[1]
redirect_uri = parse_qs(urlparse(auth_url).query).get("redirect_uri", [""])[0]
parsed_redirect = urlparse(unquote(redirect_uri))
print(parsed_redirect.port or "")
PY
}

auth_combined() {
  local log_file="/tmp/gws_auth_$(date +%s).log"
  nohup gws auth login --services "$COMBINED_SERVICES" >"$log_file" 2>&1 &
  local auth_pid=$!
  sleep 2

  local auth_url
  auth_url="$(grep -m1 -o 'https://accounts.google.com/[^[:space:]]*' "$log_file" || true)"
  if [[ -z "$auth_url" ]]; then
    echo "OAuth URL not found. Inspect: $log_file" >&2
    exit 1
  fi

  local callback_port
  callback_port="$(extract_callback_port "$auth_url")"
  echo "PID=$auth_pid"
  echo "LOG=$log_file"
  echo "URL=$auth_url"
  if [[ -n "$callback_port" ]]; then
    echo "From your workstation, keep this running before opening the URL:"
    echo "ssh -N -L ${callback_port}:127.0.0.1:${callback_port} odroid@192.168.0.11"
  else
    echo "Could not extract the callback port; inspect redirect_uri in the URL." >&2
  fi
}

heal_cache() {
  local cache_file
  for cache_file in "$USER_TOKEN_CACHE" "$SERVICE_ACCOUNT_TOKEN_CACHE"; do
    if [[ -f "$cache_file" ]]; then
      local backup_file="/tmp/$(basename "$cache_file").bak.$(date +%s)"
      cp "$cache_file" "$backup_file"
      rm -f "$cache_file"
      echo "Backed up $cache_file to $backup_file" >&2
    fi
  done
  gws auth status
}

main() {
  require_command gws
  local command_name="${1:-}"
  shift || true

  case "$command_name" in
    status) status "$@" ;;
    verify) verify "$@" ;;
    auth-combined) require_command python3; auth_combined "$@" ;;
    heal-cache) heal_cache "$@" ;;
    help|-h|--help|"") usage ;;
    *) echo "Unknown command: $command_name" >&2; usage; exit 2 ;;
  esac
}

main "$@"
