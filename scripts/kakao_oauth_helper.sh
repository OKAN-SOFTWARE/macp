#!/bin/zsh
# One-time OAuth helper to obtain Kakao access_token + refresh_token for "나에게 보내기".
#
# Prerequisites:
#   1) Register a personal Kakao Developers app: https://developers.kakao.com/console/app
#   2) Activate "Kakao Login" feature, add redirect URI: http://localhost:8765
#   3) Activate scope: talk_message
#   4) Set KAKAO_REST_API_KEY env var (REST API 키 from app settings)
#
# Usage:
#   export KAKAO_REST_API_KEY=<your-rest-api-key>
#   bash scripts/kakao_oauth_helper.sh                 # default profile
#   bash scripts/kakao_oauth_helper.sh <profile>       # a project's own app
#
# This opens a browser, captures the auth code, exchanges it for tokens, saves to
# .secrets/kakao-token.json (or kakao-token-<profile>.json).
#
# With a profile it also writes .secrets/kakao-app-<profile>.json, which is where
# kakao_token_refresh.sh reads that app's credentials from — named profiles never
# fall back to the ambient env, so without this file the token cannot be renewed.
# Set KAKAO_DISPLAY_NAME to control the sender label shown on the card.
#
# KAKAO_OAUTH_DRY_RUN=1 prints the resolved paths and exits before any browser or
# network call.

set -euo pipefail

# Two anchors, same as kakao_send.sh. ENGINE_DIR is this script's own
# checkout. VAULT_DIR is where the token and app files are written
# (.secrets/): KAKAO_VAULT_DIR (historical test seam) wins, then
# MACP_VAULT_ROOT (split install, engine checkout != vault), then the engine
# checkout itself (combined layout: unchanged behaviour).
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${KAKAO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"
SECRETS_DIR="$VAULT_DIR/.secrets"
PORT=8765
REDIRECT_URI="http://localhost:${PORT}"

# Same naming rule as kakao_send.sh / kakao_token_refresh.sh: no argument (or
# "default") keeps the original unsuffixed filenames.
PROFILE="${1:-${KAKAO_PROFILE:-default}}"
case "$PROFILE" in
  "" | *[!A-Za-z0-9_-]*)
    echo "ERROR: invalid profile name '$PROFILE' (allowed: A-Z a-z 0-9 _ -)" >&2
    exit 1
    ;;
esac

if [[ "$PROFILE" == "default" ]]; then
  TOKEN_FILE="$SECRETS_DIR/kakao-token.json"
  APP_FILE="$SECRETS_DIR/kakao-app.json"
else
  TOKEN_FILE="$SECRETS_DIR/kakao-token-$PROFILE.json"
  APP_FILE="$SECRETS_DIR/kakao-app-$PROFILE.json"
fi

if [[ -z "${KAKAO_REST_API_KEY:-}" ]]; then
  echo "ERROR: KAKAO_REST_API_KEY env var is required (REST API 키 from Kakao Developers)" >&2
  exit 1
fi

mkdir -p "$SECRETS_DIR"

if [[ "${KAKAO_OAUTH_DRY_RUN:-0}" == "1" ]]; then
  # Everything past this point opens a browser and talks to Kakao, so this is
  # the only seam tests can drive.
  echo "profile: $PROFILE"
  echo "token_file: $TOKEN_FILE"
  echo "app_file: $APP_FILE"
  echo "display_name: ${KAKAO_DISPLAY_NAME:-$PROFILE}"
  exit 0
fi

AUTH_URL="https://kauth.kakao.com/oauth/authorize?response_type=code&client_id=${KAKAO_REST_API_KEY}&redirect_uri=${REDIRECT_URI}&scope=talk_message"

echo "1) Opening browser to authorize..."
echo "   If browser does not open, manually visit:"
echo "   $AUTH_URL"
echo ""

# Background a tiny Python HTTP server that captures the code parameter.
# (There used to be a second, scriptless `python3 -` spawned here whose
# SERVER_PID was immediately overwritten below — it read the enclosing script as
# its stdin and served nothing. Removed.)
CODE_FILE="$(mktemp -t kakao-code.XXXXXX)"

cat <<'PY' | /usr/bin/python3 - "$PORT" "$CODE_FILE" &
import sys, http.server, urllib.parse
port = int(sys.argv[1])
code_file = sys.argv[2]

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(qs))
        code = params.get("code", "")
        if code:
            with open(code_file, "w") as f:
                f.write(code)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "인증 완료 — 터미널로 돌아가세요." if code else "인증 실패"
        self.wfile.write(f"<html><body><h1>{msg}</h1></body></html>".encode("utf-8"))

http.server.HTTPServer(("127.0.0.1", port), H).handle_request()
PY
SERVER_PID=$!

# Open the auth URL in the default browser.
open "$AUTH_URL" || true

# Wait for the code to appear.
echo "2) Waiting for authorization (complete login in browser)..."
for _ in $(seq 1 120); do
  if [[ -s "$CODE_FILE" ]]; then
    break
  fi
  sleep 1
done

wait "$SERVER_PID" 2>/dev/null || true

CODE="$(cat "$CODE_FILE")"
rm -f "$CODE_FILE"

if [[ -z "$CODE" ]]; then
  echo "ERROR: no auth code captured within 120s" >&2
  exit 1
fi

echo "3) Got auth code (${#CODE} chars). Exchanging for tokens..."

# Build curl args; include client_secret only if provided.
TOKEN_ARGS=(
  -sS -X POST "https://kauth.kakao.com/oauth/token"
  -H "Content-Type: application/x-www-form-urlencoded"
  --data-urlencode "grant_type=authorization_code"
  --data-urlencode "client_id=${KAKAO_REST_API_KEY}"
  --data-urlencode "redirect_uri=${REDIRECT_URI}"
  --data-urlencode "code=${CODE}"
)
if [[ -n "${KAKAO_CLIENT_SECRET:-}" ]]; then
  TOKEN_ARGS+=(--data-urlencode "client_secret=${KAKAO_CLIENT_SECRET}")
fi

RESPONSE="$(curl "${TOKEN_ARGS[@]}")"

# Validate JSON has access_token
if ! /usr/bin/python3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert 'access_token' in d, d" <<<"$RESPONSE" 2>/dev/null; then
  echo "ERROR: token exchange failed — response: $RESPONSE" >&2
  exit 1
fi

# Save with restrictive permissions.
echo "$RESPONSE" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

# Persist the app credentials alongside the token. kakao_token_refresh.sh reads
# this file to renew the token, and for a named profile it is the *only* source
# — ambient env belongs to the default app and is deliberately not inherited.
# Merge rather than overwrite so a hand-added field survives re-running this.
export KAKAO_APP_PROFILE="$PROFILE"
export KAKAO_REST_API_KEY
export KAKAO_CLIENT_SECRET="${KAKAO_CLIENT_SECRET:-}"
export KAKAO_DISPLAY_NAME="${KAKAO_DISPLAY_NAME:-}"
/usr/bin/python3 - "$APP_FILE" <<'PY'
import json, os, sys

path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
except (OSError, ValueError):
    data = {}

data["rest_api_key"] = os.environ["KAKAO_REST_API_KEY"]
secret = os.environ.get("KAKAO_CLIENT_SECRET", "")
if secret:
    data["client_secret"] = secret

display_name = os.environ.get("KAKAO_DISPLAY_NAME", "")
if display_name:
    data["display_name"] = display_name
else:
    data.setdefault("display_name", os.environ["KAKAO_APP_PROFILE"])

with open(path, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
PY
chmod 600 "$APP_FILE"

echo "4) Saved token: $TOKEN_FILE"
echo "   Saved app:   $APP_FILE"
echo ""
if [[ "$PROFILE" == "default" ]]; then
  echo "Test it with:"
  echo "  bash scripts/notify_kakao.sh"
else
  echo "Test it with:"
  echo "  KAKAO_PROFILE=$PROFILE bash scripts/kakao_send.sh \"테스트 메시지\""
fi
