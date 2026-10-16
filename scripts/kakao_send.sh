#!/bin/zsh
# Send arbitrary text via KakaoTalk "나에게 보내기" memo API.
#
# Usage:  scripts/kakao_send.sh "<message text>" [link_url]
#         KAKAO_PROFILE=<name> scripts/kakao_send.sh "<message text>" [link_url]
#
# - Reads access_token from .secrets/kakao-token.json (rotated by kakao_token_refresh.sh).
# - KAKAO_PROFILE=<name> instead reads .secrets/kakao-token-<name>.json and sends a
#   `feed` card whose profile_text is that app's display_name, so each project shows a
#   distinct sender. Missing profile token → warn + fall back to default (see KAKAO_SETUP.md).
# - On 401, refreshes the token once and retries — same self-heal pattern as notify_kakao.sh.
# - Non-fatal: prints error to stderr but exits 0 if API call fails, so callers
#   (e.g. worker stages) don't break on transient kakao issues. Override with
#   KAKAO_SEND_STRICT=1 to make failures fatal.
# - Noise tiers (OPT-IN): KAKAO_LEVEL=digest|silent routes via scripts/kakao_route.py
#   (digest → queued for the daily summary, silent → log only). KAKAO_EVENT_KEY=<key>
#   merges duplicate immediate alerts inside a short window. BOTH unset (every
#   pre-existing caller, external repos included) = this block is never entered
#   and the send path is unchanged. Tier table: scripts/KAKAO_ALERTS.md.

set -uo pipefail

# Two anchors. ENGINE_DIR is this script's own checkout: sibling engine
# scripts (kakao_route.py, kakao_token_refresh.sh) and the todo_system
# package live there. VAULT_DIR is where the data lives (.secrets/, logs/,
# .macp/config.toml): KAKAO_VAULT_DIR (historical test seam) wins, then
# MACP_VAULT_ROOT (split install, engine checkout != vault), then the
# engine checkout itself (combined layout: unchanged behaviour).
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${KAKAO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"
SECRETS_DIR="$VAULT_DIR/.secrets"
LOG_DIR="$VAULT_DIR/logs"
NOW_TS="$(date +%FT%T)"

# Interpreter for the two calls that import todo_system from the engine (the
# repo-slug resolver and kakao_route.py). Installed wheel (public split 13b):
# ENGINE_DIR is site-packages/todo_system/_engine, which holds no todo_system
# package, so engine_script hands the caller's interpreter down as MACP_PYTHON
# and those calls run under it with -P (its site-packages already holds the
# package). Only when BOTH hold — no pyproject.toml in ENGINE_DIR AND
# MACP_PYTHON set; otherwise /usr/bin/python3 with ENGINE_DIR on sys.path, as
# before. The stdlib-only JSON/template helpers stay on /usr/bin/python3.
ENGINE_PY=(/usr/bin/python3)
if [[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]; then
  ENGINE_PY=("$MACP_PYTHON" -P)
fi

mkdir -p "$LOG_DIR"

log() {
  echo "[$NOW_TS] kakao_send: $*" | tee -a "$LOG_DIR/kakao_send.out.log" >&2
}

if [[ $# -lt 1 ]]; then
  log "ERROR usage: $0 \"<message>\" [link_url]"
  exit 1
fi

MESSAGE="$1"
LINK_URL="${2:-}"
# The *explicit* 2nd arg only — the default repo card link below is Kakao-only,
# so Slack appends ` <url|열기>` solely when the caller passed a link.
EXPLICIT_LINK="${2:-}"
if [[ -z "$LINK_URL" ]]; then
  # Default card link = this vault's own repo, through the shared resolver
  # (env MACP_REPO_SLUG -> $VAULT_DIR/.macp/config.toml repo_slug -> neutral
  # placeholder). The engine is imported from ENGINE_DIR, the config is read
  # from VAULT_DIR.
  REPO_SLUG="$("${ENGINE_PY[@]}" -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from todo_system import paths; print(paths.repo_slug(start=Path(sys.argv[2])))' "$ENGINE_DIR" "$VAULT_DIR" 2>/dev/null || true)"
  LINK_URL="https://github.com/${REPO_SLUG:-example-org/MaestroACP}"
fi

# --- noise-tier routing (opt-in via env; see header) ------------------------
# Runs BEFORE token checks on purpose: a digest/silent alert must be recorded
# even when no token is available, or a token outage would silently drop it.
if [[ -n "${KAKAO_LEVEL:-}${KAKAO_EVENT_KEY:-}" ]]; then
  ROUTE_PY="$ENGINE_DIR/scripts/kakao_route.py"
  if [[ -f "$ROUTE_PY" ]]; then
    # First stdout line is the verdict; a SEND verdict is followed by the final
    # message (may carry an "외 N건" merge suffix). The shim exits 0 and prints
    # SEND + the original message on any internal failure (fail-open).
    ROUTE_OUT="$(KAKAO_VAULT_DIR="$VAULT_DIR" "${ENGINE_PY[@]}" "$ROUTE_PY" "$MESSAGE" 2>/dev/null || true)"
    VERDICT="${ROUTE_OUT%%$'\n'*}"
    case "$VERDICT" in
      QUEUED | SILENT | SUPPRESSED)
        log "route: $VERDICT (level=${KAKAO_LEVEL:-immediate}): ${MESSAGE:0:80}"
        exit 0
        ;;
      SEND)
        if [[ "$ROUTE_OUT" != "$VERDICT" ]]; then
          MESSAGE="${ROUTE_OUT#*$'\n'}"
        fi
        ;;
      *)
        # Unreadable verdict (python missing/crashed): deliver rather than lose.
        log "WARN route verdict unreadable — sending immediately"
        ;;
    esac
  fi
fi

# --- backend selection + fanout ---------------------------------------------
# Routing above already fired once (and a QUEUED/SILENT/SUPPRESSED verdict has
# already exited), so from here the ROUTED $MESSAGE is dispatched to each
# selected backend. NOTIFY_BACKENDS (comma-separated) chooses them explicitly.
# Since the Slack-only cutover the DEFAULT (NOTIFY_BACKENDS unset) is:
#   * Slack secret present ($SECRETS_DIR/notify-slack.json) → "slack" ALONE
#     (zero Kakao HTTP — Slack is the live channel now);
#   * no Slack secret → "kakao" fallback, so a host without Slack never loses an
#     alert (request body and exit code byte-identical to the pre-cutover path).
# Rollback is env-only: NOTIFY_BACKENDS=kakao re-enables Kakao, =kakao,slack fans
# out to both. STRICT success is defined against the PRIMARY backend = the first
# entry of the list.
if [[ -n "${NOTIFY_BACKENDS:-}" ]]; then
  BACKENDS="$NOTIFY_BACKENDS"
elif [[ -f "$SECRETS_DIR/notify-slack.json" ]]; then
  BACKENDS="slack"
else
  BACKENDS="kakao"
fi
PRIMARY="${BACKENDS%%,*}"
# Normalised haystack ",kakao,slack," makes membership an exact glob test in
# both bash and zsh (no array indexing, which differs between the shells).
BACKENDS_CSV=",$BACKENDS,"

# Slack backend (non-fatal for a non-primary backend). slack_send.py inherits
# KAKAO_PROFILE / NOTIFY_CHANNEL_KEY / KAKAO_VAULT_DIR / MACP_VAULT_ROOT from the
# environment; only the explicit link is forwarded. Its webhook URL is a secret,
# so its stderr (already URL-masked) is the WARN detail, never the URL.
SLACK_SENT_RC=2   # 2 = not attempted
if [[ "$BACKENDS_CSV" == *,slack,* ]]; then
  SLACK_SEND_PY="$ENGINE_DIR/scripts/slack_send.py"
  if [[ -f "$SLACK_SEND_PY" ]]; then
    SLACK_ERR="$(/usr/bin/python3 "$SLACK_SEND_PY" "$MESSAGE" "$EXPLICIT_LINK" 2>&1)"
    SLACK_SENT_RC=$?
    if [[ "$SLACK_SENT_RC" != "0" ]]; then
      log "WARN slack: ${SLACK_ERR:-send failed}"
    fi
  else
    log "WARN slack backend requested but slack_send.py missing: $SLACK_SEND_PY"
    SLACK_SENT_RC=1
  fi
fi

# Kakao backend. The original send path (profile resolution → token → template →
# curl → 401 retry → exit) is wrapped UNCHANGED in a subshell forced to
# KAKAO_SEND_STRICT=1 so its exit is a clean 0/1 success signal we capture; the
# caller's real STRICT is applied once in finish below. `exit` inside `( )` ends
# only the subshell in both bash and zsh, so the raw rc is captured, not
# propagated. kakao ∉ backends → the whole block is skipped.
KAKAO_RC=2   # 2 = not attempted
if [[ "$BACKENDS_CSV" == *,kakao,* ]]; then
(
KAKAO_SEND_STRICT=1
# --- profile resolution -----------------------------------------------------
# KAKAO_PROFILE=<name> sends through a separate Kakao app so the message carries
# that project's own sender identity. Unset (or "default") is the original
# behaviour, byte for byte: kakao-token.json + the plain text template.
PROFILE="${KAKAO_PROFILE:-default}"

# A profile name is interpolated straight into a .secrets/ path, so anything
# outside [A-Za-z0-9_-] (notably "/" and "..") is refused rather than allowed to
# walk out of the directory.
case "$PROFILE" in
  "" | *[!A-Za-z0-9_-]*)
    log "WARN invalid KAKAO_PROFILE '$PROFILE' — falling back to default"
    PROFILE="default"
    ;;
esac

if [[ "$PROFILE" == "default" ]]; then
  TOKEN_FILE="$SECRETS_DIR/kakao-token.json"
  APP_FILE="$SECRETS_DIR/kakao-app.json"
else
  TOKEN_FILE="$SECRETS_DIR/kakao-token-$PROFILE.json"
  APP_FILE="$SECRETS_DIR/kakao-app-$PROFILE.json"
  if [[ ! -f "$TOKEN_FILE" ]]; then
    # Fall all the way back to the default identity — sending a feed whose
    # profile_text names another project on the *default* app's token would
    # mislabel the sender rather than degrade gracefully.
    log "WARN profile '$PROFILE' token missing: $TOKEN_FILE — falling back to default"
    PROFILE="default"
    TOKEN_FILE="$SECRETS_DIR/kakao-token.json"
    APP_FILE="$SECRETS_DIR/kakao-app.json"
  fi
fi

# Shown as the card title and profile_text. A missing app file is *not* a
# fallback trigger: the token is still the profile's, so only the label degrades.
DISPLAY_NAME=""
if [[ "$PROFILE" != "default" ]]; then
  if [[ -f "$APP_FILE" ]]; then
    DISPLAY_NAME="$(/usr/bin/python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('display_name',''))" "$APP_FILE" 2>/dev/null || true)"
  fi
  if [[ -z "$DISPLAY_NAME" ]]; then
    DISPLAY_NAME="$PROFILE"
  fi
fi

if [[ ! -f "$TOKEN_FILE" ]]; then
  log "WARN token file missing: $TOKEN_FILE (skipping)"
  [[ "${KAKAO_SEND_STRICT:-0}" == "1" ]] && exit 1 || exit 0
fi

read_access_token() {
  /usr/bin/python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['access_token'])" "$TOKEN_FILE"
}

ACCESS_TOKEN="$(read_access_token 2>/dev/null || true)"
if [[ -z "$ACCESS_TOKEN" ]]; then
  log "WARN could not read access_token (skipping)"
  [[ "${KAKAO_SEND_STRICT:-0}" == "1" ]] && exit 1 || exit 0
fi

export MSG="$MESSAGE"
export URL="$LINK_URL"
export KAKAO_TPL_PROFILE="$PROFILE"
export KAKAO_TPL_DISPLAY_NAME="$DISPLAY_NAME"
# The last-resort cut below is now logged when it fires. Length is measured in
# python (code points, matching the actual cut) rather than bash ${#} to dodge
# the byte/char ambiguity under the C locale, and the WARN line is appended to
# the same log the log() helper writes.
export KAKAO_LOG_FILE="$LOG_DIR/kakao_send.out.log"
export KAKAO_NOW_TS="$NOW_TS"
# NOTE: this heredoc sits inside a $( ) command substitution, which bash scans
# for the closing paren *through* the heredoc body. A lone apostrophe in here —
# even in a comment, even though the delimiter is quoted as <<'PY' — unbalances
# that scan and breaks the whole script. Keep apostrophes paired or absent.
TEMPLATE_OBJECT="$(/usr/bin/python3 - <<'PY'
import json, os
message = os.environ['MSG']
url = os.environ['URL']
profile = os.environ['KAKAO_TPL_PROFILE']
display_name = os.environ['KAKAO_TPL_DISPLAY_NAME']
# Kakao memo text ~200자 한도 (한국어 포함), 180자 안전 margin.
# The feed description limit is undocumented and cannot be measured without
# live API access, so the same conservative cut is applied to both templates.
# Callers self-limit before reaching here; this blind cut is the last-resort net.
# When it fires we log WARN truncated <원래길이>→180 so it is no longer silent.
if len(message) > 180:
    text = message[:179] + "…"
    try:
        with open(os.environ["KAKAO_LOG_FILE"], "a", encoding="utf-8") as fh:
            fh.write("[" + os.environ["KAKAO_NOW_TS"] + "] kakao_send: WARN truncated "
                     + str(len(message)) + "→180\n")
    except OSError:
        pass
else:
    text = message
link = {
    "web_url": url,
    "mobile_web_url": url,
}
if profile == "default":
    template = {
        "object_type": "text",
        "text": text,
        "link": link,
        "button_title": "열기",
    }
else:
    # feed carries item_content.profile_text, which is what renders the
    # per-project sender label at the top of the card.
    template = {
        "object_type": "feed",
        "content": {
            "title": display_name,
            "description": text,
            "link": link,
        },
        "item_content": {
            "profile_text": display_name,
        },
        "buttons": [
            {"title": "열기", "link": link},
        ],
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
BODY="$(echo "$RESPONSE" | grep -v '^HTTP_CODE:')"

if [[ "$HTTP_CODE" == "200" ]]; then
  log "sent (200): ${MESSAGE:0:80}"
  exit 0
fi

# 401 → refresh token + retry once
if [[ "$HTTP_CODE" == "401" ]]; then
  log "401 — refreshing token and retrying"
  # Pass the profile explicitly. Left to inherit an unset KAKAO_PROFILE the
  # refresh script would run in loop-all mode, whose exit code reports "some
  # profile succeeded" — not "this token was renewed", which is what the retry
  # below depends on. The vault is passed explicitly too, so the refresh uses
  # exactly this script's data anchor.
  if KAKAO_PROFILE="$PROFILE" KAKAO_VAULT_DIR="$VAULT_DIR" bash "$ENGINE_DIR/scripts/kakao_token_refresh.sh" >/dev/null 2>&1; then
    ACCESS_TOKEN="$(read_access_token 2>/dev/null || true)"
    if [[ -n "$ACCESS_TOKEN" ]]; then
      RESPONSE="$(send_memo "$ACCESS_TOKEN")"
      HTTP_CODE="$(echo "$RESPONSE" | awk -F: '/^HTTP_CODE:/ { print $2 }')"
      BODY="$(echo "$RESPONSE" | grep -v '^HTTP_CODE:')"
      if [[ "$HTTP_CODE" == "200" ]]; then
        log "sent after refresh (200): ${MESSAGE:0:80}"
        exit 0
      fi
    fi
  fi
fi

log "ERROR HTTP $HTTP_CODE — body: $BODY"
[[ "${KAKAO_SEND_STRICT:-0}" == "1" ]] && exit 1 || exit 0
)
KAKAO_RC=$?
fi

# --- finish: apply the caller's real STRICT against the PRIMARY backend ------
# Since the cutover the DEFAULT (unset NOTIFY_BACKENDS, Slack secret present) is
# PRIMARY=slack, so Slack's success drives STRICT. A no-secret host defaults to
# PRIMARY=kakao (the fallback), which reproduces the pre-dispatcher behaviour
# exactly — STRICT ? (kakao succeeded ? 0 : 1) : 0. An explicit NOTIFY_BACKENDS
# puts whichever backend it lists first in the PRIMARY slot.
if [[ "$PRIMARY" == "slack" ]]; then
  [[ "$SLACK_SENT_RC" == "0" ]] && PRIMARY_OK=1 || PRIMARY_OK=0
else
  [[ "$KAKAO_RC" == "0" ]] && PRIMARY_OK=1 || PRIMARY_OK=0
fi

if [[ "${KAKAO_SEND_STRICT:-0}" == "1" && "$PRIMARY_OK" != "1" ]]; then
  exit 1
fi
exit 0
