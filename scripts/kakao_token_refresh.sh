#!/bin/zsh
# Refresh Kakao access_token(s) using the stored refresh_token.
#
# Usage:
#   kakao_token_refresh.sh                     # every profile found in .secrets/
#   kakao_token_refresh.sh <profile>           # only that profile
#   KAKAO_PROFILE=<profile> kakao_token_refresh.sh
#
# refresh_token itself expires after ~60 days — when that happens, re-run
# kakao_oauth_helper.sh (with the same profile name).

set -euo pipefail

# Two anchors, same as kakao_send.sh. ENGINE_DIR is this script's own
# checkout. VAULT_DIR is where the data lives (.secrets/, logs/):
# KAKAO_VAULT_DIR (historical test seam) wins, then MACP_VAULT_ROOT (split
# install, engine checkout != vault), then the engine checkout itself
# (combined layout: unchanged behaviour).
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${KAKAO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"
SECRETS_DIR="$VAULT_DIR/.secrets"
LOG_DIR="$VAULT_DIR/logs"
NOW_TS="$(date +%FT%T)"

mkdir -p "$LOG_DIR"

log() {
  echo "[$NOW_TS] kakao_token_refresh: $*" | tee -a "$LOG_DIR/notify_kakao.out.log"
}

read_json_field() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2"
}

# Merge new fields into the existing token file (refresh_token is not returned
# every time, so a plain overwrite would throw it away).
merge_token() {
  /usr/bin/python3 - "$1" "$2" <<'PY'
import json, sys
token_path = sys.argv[1]
response_raw = sys.argv[2]
try:
    response = json.loads(response_raw)
except ValueError:
    print(f"refresh failed (response was not JSON): {response_raw}", file=sys.stderr)
    sys.exit(1)
if "access_token" not in response:
    print(f"refresh failed: {response_raw}", file=sys.stderr)
    sys.exit(1)
with open(token_path) as f:
    existing = json.load(f)
existing.update(response)
with open(token_path, "w") as f:
    json.dump(existing, f, ensure_ascii=False, indent=2)
PY
}

refresh_one() {
  local profile="$1"
  local token_file app_file rest_api_key client_secret refresh_token response
  local -a args

  if [[ "$profile" == "default" ]]; then
    token_file="$SECRETS_DIR/kakao-token.json"
    app_file="$SECRETS_DIR/kakao-app.json"
    # Ambient KAKAO_REST_API_KEY / KAKAO_CLIENT_SECRET belong to the DEFAULT app
    # — the refresh LaunchAgent plist exports exactly those two. Letting them win
    # here preserves the pre-profile contract byte for byte.
    rest_api_key="${KAKAO_REST_API_KEY:-}"
    client_secret="${KAKAO_CLIENT_SECRET:-}"
  else
    token_file="$SECRETS_DIR/kakao-token-$profile.json"
    app_file="$SECRETS_DIR/kakao-app-$profile.json"
    # ...and they must never reach a named profile. Refreshing kakao-token-cgv
    # with the default app client_id makes Kakao reject the call, so that token
    # would silently stop rotating and die at the next ~6h expiry. Named
    # profiles read credentials only from their own app file.
    rest_api_key=""
    client_secret=""
  fi

  if [[ -z "$rest_api_key" && -f "$app_file" ]]; then
    rest_api_key="$(read_json_field "$app_file" rest_api_key 2>/dev/null || true)"
  fi
  if [[ -z "$client_secret" && -f "$app_file" ]]; then
    client_secret="$(read_json_field "$app_file" client_secret 2>/dev/null || true)"
  fi

  if [[ -z "$rest_api_key" ]]; then
    log "ERROR [$profile] no rest_api_key — env unset and $app_file missing or incomplete"
    return 1
  fi

  if [[ ! -f "$token_file" ]]; then
    log "ERROR [$profile] token file missing: $token_file"
    return 1
  fi

  refresh_token="$(read_json_field "$token_file" refresh_token 2>/dev/null || true)"
  if [[ -z "$refresh_token" ]]; then
    log "ERROR [$profile] no refresh_token in $token_file"
    return 1
  fi

  args=(
    -sS -X POST "https://kauth.kakao.com/oauth/token"
    -H "Content-Type: application/x-www-form-urlencoded"
    --data-urlencode "grant_type=refresh_token"
    --data-urlencode "client_id=${rest_api_key}"
    --data-urlencode "refresh_token=${refresh_token}"
  )
  if [[ -n "$client_secret" ]]; then
    args+=(--data-urlencode "client_secret=${client_secret}")
  fi

  if ! response="$(curl "${args[@]}")"; then
    log "ERROR [$profile] refresh request failed"
    return 1
  fi

  if ! merge_token "$token_file" "$response"; then
    log "ERROR [$profile] refresh rejected — if refresh_token expired, re-run: bash scripts/kakao_oauth_helper.sh $profile"
    return 1
  fi

  chmod 600 "$token_file"
  log "[$profile] refreshed access_token"
  return 0
}

PROFILE_ARG="${1:-${KAKAO_PROFILE:-}}"

if [[ -n "$PROFILE_ARG" ]]; then
  # Single-profile mode. kakao_send.sh always calls this way, so its 401
  # self-heal keeps the exact exit-code meaning it had before profiles existed:
  # 0 means "this token was renewed", not "something somewhere was renewed".
  if refresh_one "$PROFILE_ARG"; then
    exit 0
  fi
  exit 1
fi

# Loop mode — this is what the 4-hourly LaunchAgent runs.
# Discovery uses find rather than a bare glob: an unmatched glob is a *fatal*
# error under zsh, which is the production shell for this script.
TOKEN_FILES=()
while IFS= read -r token_path; do
  TOKEN_FILES+=("$token_path")
done < <(find "$SECRETS_DIR" -maxdepth 1 -name 'kakao-token*.json' -type f 2>/dev/null | sort)

if [[ ${#TOKEN_FILES[@]} -eq 0 ]]; then
  log "ERROR no kakao-token*.json found in $SECRETS_DIR"
  exit 1
fi

OK_COUNT=0
FAIL_COUNT=0
for token_path in "${TOKEN_FILES[@]}"; do
  base="${token_path##*/}"
  case "$base" in
    kakao-token.json)
      profile="default"
      ;;
    kakao-token-*.json)
      profile="${base#kakao-token-}"
      profile="${profile%.json}"
      ;;
    *)
      continue
      ;;
  esac
  # Calling through an `if` condition suspends `set -e` for the whole call, so
  # one dead profile cannot abort the profiles queued behind it.
  if refresh_one "$profile"; then
    OK_COUNT=$((OK_COUNT + 1))
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
done

log "summary: $OK_COUNT refreshed, $FAIL_COUNT failed"

# Exit 0 when at least one profile refreshed. A stale side project must not mark
# the LaunchAgent job as failed; only a total wipeout is worth alerting on.
if [[ "$OK_COUNT" -ge 1 ]]; then
  exit 0
fi
exit 1
