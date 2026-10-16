#!/bin/zsh
# Send today's brief summary to KakaoTalk "나에게 보내기" (memo) API.
# Reads Todo/Briefings/<today>.md, extracts the one-line summary, POSTs to Kakao.
#
# Setup (one-time, by user):
#   1) Register a personal app at https://developers.kakao.com/console/app
#   2) Enable "카카오 로그인" + "카카오톡 메시지" scope (talk_message)
#   3) Set redirect URI to http://localhost:8765 (matches kakao_oauth_helper.sh)
#   4) Run scripts/kakao_oauth_helper.sh to obtain initial token
#   5) Add this script to brief.sh tail (or run independently)
#
# Token rotation: refresh_token expires after ~60 days. Run kakao_token_refresh.sh weekly.

set -euo pipefail

# Two anchors, same as kakao_send.sh (never a hardcoded path: that once made
# the push a silent no-op on another host). ENGINE_DIR is this script's own
# checkout: _build_brief_message.py, kakao_token_refresh.sh and the
# todo_system package live there. VAULT_DIR is where the data lives
# (.secrets/, logs/, Todo/Briefings/, .macp/config.toml): KAKAO_VAULT_DIR
# (historical test seam) wins, then MACP_VAULT_ROOT (split install, engine
# checkout != vault), then the engine checkout itself (combined layout:
# unchanged behaviour).
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${KAKAO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"
TOKEN_FILE="$VAULT_DIR/.secrets/kakao-token.json"
LOG_DIR="$VAULT_DIR/logs"
TODAY="$(date +%F)"
BRIEF_FILE="$VAULT_DIR/Todo/Briefings/${TODAY}.md"
NOW_TS="$(date +%FT%T)"

# Interpreter for the repo-slug resolver, which imports todo_system from the
# engine. Installed wheel (public split 13b): ENGINE_DIR is site-packages/
# todo_system/_engine, which holds no todo_system package, so engine_script
# hands the caller's interpreter down as MACP_PYTHON and that call runs under
# it with -P (its site-packages already holds the package). Only when BOTH
# hold — no pyproject.toml in ENGINE_DIR AND MACP_PYTHON set; otherwise
# /usr/bin/python3 with ENGINE_DIR on sys.path, as before. The stdlib-only
# helpers (token read, _build_brief_message.py, template) stay on /usr/bin/python3.
ENGINE_PY=(/usr/bin/python3)
if [[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]; then
  ENGINE_PY=("$MACP_PYTHON" -P)
fi

mkdir -p "$LOG_DIR"

log() {
  echo "[$NOW_TS] notify_kakao: $*" | tee -a "$LOG_DIR/notify_kakao.out.log"
}

# --- backend selection (same rule as kakao_send.sh) -------------------------
# Since the Slack-only cutover: unset NOTIFY_BACKENDS + Slack secret present →
# "slack" alone; no secret → "kakao" fallback (so the brief is never lost on a
# host without Slack); NOTIFY_BACKENDS=kakao rolls back, =kakao,slack fans out.
# Chosen BEFORE the Kakao token gate so a Slack-only host that has no Kakao token
# can still send the brief.
if [[ -n "${NOTIFY_BACKENDS:-}" ]]; then
  BRIEF_BACKENDS="$NOTIFY_BACKENDS"
elif [[ -f "$VAULT_DIR/.secrets/notify-slack.json" ]]; then
  BRIEF_BACKENDS="slack"
else
  BRIEF_BACKENDS="kakao"
fi

# The brief file is the message SOURCE for every backend, so this check is
# backend-independent and stays up front.
if [[ ! -f "$BRIEF_FILE" ]]; then
  log "ERROR brief file missing for today: $BRIEF_FILE — run brief.sh first"
  exit 1
fi

read_access_token() {
  /usr/bin/python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['access_token'])" "$TOKEN_FILE"
}
# NOTE: the Kakao token file check + access_token read are deferred into the
# kakao-guarded block below — a Slack-only host need not have a Kakao token.

# Build a compact message that fits inside Kakao's ~200-char text limit.
# Logic lives in scripts/_build_brief_message.py (an engine file) — see that
# file for detail.
export BRIEF_FILE TODAY
MESSAGE_TEXT="$(/usr/bin/python3 "$ENGINE_DIR/scripts/_build_brief_message.py")"
if [[ -z "$MESSAGE_TEXT" ]]; then
  MESSAGE_TEXT="📋 ${TODAY} Brief

오늘의 brief가 준비되었습니다."
fi

# Card link = this vault's own repo (env MACP_REPO_SLUG -> .macp/config.toml
# repo_slug -> neutral placeholder), same resolver as kakao_send.sh. The engine
# is imported from ENGINE_DIR, the config is read from VAULT_DIR.
REPO_SLUG="$("${ENGINE_PY[@]}" -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from todo_system import paths; print(paths.repo_slug(start=Path(sys.argv[2])))' "$ENGINE_DIR" "$VAULT_DIR" 2>/dev/null || true)"
BRIEF_URL="https://github.com/${REPO_SLUG:-example-org/MaestroACP}"

# --- Slack send (backend already chosen above) ------------------------------
# The brief rides its own 'default' channel. Non-fatal by construction: under
# `set -e` a Slack failure must never take down the brief, so the call is run
# inside an `if` (which suspends set -e) and only ever produces a WARN. The
# webhook URL is secret, so only slack_send's already-masked stderr is logged.
if [[ ",$BRIEF_BACKENDS," == *,slack,* ]]; then
  SLACK_SEND_PY="$ENGINE_DIR/scripts/slack_send.py"
  if [[ -f "$SLACK_SEND_PY" ]]; then
    if SLACK_ERR="$(NOTIFY_CHANNEL_KEY=default /usr/bin/python3 "$SLACK_SEND_PY" "$MESSAGE_TEXT" "$BRIEF_URL" 2>&1)"; then
      log "slack brief sent (default channel)"
    else
      log "WARN slack brief: ${SLACK_ERR:-send failed}"
    fi
  fi
fi

# --- Kakao send (rollback path; skipped entirely on a Slack-only host) -------
# kakao ∉ BRIEF_BACKENDS → no Kakao token is required and nothing below runs, so
# a Slack-only host without a Kakao token still exits cleanly after the Slack
# send. The Kakao token gate is deferred to here (not the top) for that reason.
if [[ ",$BRIEF_BACKENDS," == *,kakao,* ]]; then

if [[ ! -f "$TOKEN_FILE" ]]; then
  log "ERROR token file missing: $TOKEN_FILE — run kakao_oauth_helper.sh first"
  exit 1
fi
ACCESS_TOKEN="$(read_access_token)"
if [[ -z "$ACCESS_TOKEN" ]]; then
  log "ERROR could not read access_token from $TOKEN_FILE"
  exit 1
fi

export MSG="$MESSAGE_TEXT"
export URL="$BRIEF_URL"
TEMPLATE_OBJECT="$(/usr/bin/python3 - <<'PY'
import json, os
message = os.environ['MSG']
url = os.environ['URL']
template = {
    "object_type": "text",
    "text": message,
    "link": {
        "web_url": url,
        "mobile_web_url": url,
    },
    "button_title": "Brief 열기",
}
print(json.dumps(template, ensure_ascii=False))
PY
)"

send_memo() {
  curl -sS -X POST "https://kapi.kakao.com/v2/api/talk/memo/default/send" \
    -H "Authorization: Bearer $1" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "template_object=${TEMPLATE_OBJECT}" \
    -w "\nHTTP_CODE:%{http_code}"
}

RESPONSE="$(send_memo "$ACCESS_TOKEN")"

HTTP_CODE="$(echo "$RESPONSE" | awk -F: '/^HTTP_CODE:/ { print $2 }')"
BODY="$(echo "$RESPONSE" | grep -v "^HTTP_CODE:")"

if [[ "$HTTP_CODE" == "200" ]]; then
  log "sent (http 200): text=${MESSAGE_TEXT:0:80}..."
  exit 0
fi

# On 401, refresh token once and retry.
if [[ "$HTTP_CODE" == "401" ]]; then
  log "access_token rejected (401) — refreshing and retrying"
  # Pin the profile: this script only ever sends on the default token, and a
  # bare call now means "refresh every profile", whose exit code reports that
  # *some* profile succeeded. That would let a healthy side profile mask a dead
  # default token and send the retry below on the same stale credential.
  # KAKAO_VAULT_DIR pins the refresh to exactly this script's data anchor.
  if KAKAO_PROFILE=default KAKAO_VAULT_DIR="$VAULT_DIR" bash "$ENGINE_DIR/scripts/kakao_token_refresh.sh"; then
    ACCESS_TOKEN="$(read_access_token)"
    RESPONSE="$(send_memo "$ACCESS_TOKEN")"
    HTTP_CODE="$(echo "$RESPONSE" | awk -F: '/^HTTP_CODE:/ { print $2 }')"
    BODY="$(echo "$RESPONSE" | grep -v "^HTTP_CODE:")"
    if [[ "$HTTP_CODE" == "200" ]]; then
      log "sent after refresh (http 200): text=${MESSAGE_TEXT:0:80}..."
      exit 0
    fi
  else
    log "ERROR token refresh failed"
  fi
fi

log "ERROR Kakao API returned HTTP $HTTP_CODE — body: $BODY"

exit 1
fi  # end kakao backend guard

# Reached only on the Slack-only path (kakao not selected): the brief has already
# gone out via Slack (or produced a non-fatal WARN), so finish cleanly.
exit 0
