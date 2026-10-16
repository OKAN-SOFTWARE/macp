#!/bin/zsh
# Install a LaunchAgent that refreshes the Kakao access_token every 4 hours.
# access_token lasts ~6h, so a 4h cadence keeps a fresh token ready and prevents
# the refresh_token (~60 day) from expiring. RunAtLoad also refreshes on login.
#
# DEFAULT-OFF since the Slack-only cutover: Kakao is a rollback-only backend, so
# this installer refuses to install unless opted in with KAKAO_REFRESH_INSTALL=1.
# This mirrors gen_units.py's INSTALL_ONLY exclusion of macp-kakao-refresh.
#
# Requires KAKAO_REST_API_KEY to be set somewhere the LaunchAgent can read it.
# We embed it in the plist as EnvironmentVariables — supply it via env to this installer.

set -euo pipefail

# Opt-in guard (default-OFF). A plain run is a no-op success, not an error — the
# default is "don't install", so returning 0 keeps callers/pipelines happy.
if [[ "${KAKAO_REFRESH_INSTALL:-}" != "1" ]]; then
  echo "카톡 토큰 갱신은 Slack 단독 전환으로 기본 비활성입니다 (설치하지 않음)." >&2
  echo "정말 설치하려면: KAKAO_REFRESH_INSTALL=1 KAKAO_REST_API_KEY=<key> bash $0" >&2
  exit 0
fi

if [[ -z "${KAKAO_REST_API_KEY:-}" ]]; then
  echo "ERROR KAKAO_REST_API_KEY env var is required" >&2
  echo "Run: export KAKAO_REST_API_KEY=<key> && bash $0" >&2
  exit 1
fi

VAULT_DIR="${MACP_VAULT_ROOT:-$HOME/Projects/MaestroACP}"
LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.kakao-refresh"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$VAULT_DIR/logs"
SCRIPT_PATH="$VAULT_DIR/scripts/kakao_token_refresh.sh"

# Installed mode (public split 13d, as gen_units does in 13c): with
# MACP_ENGINE_PYTHON set, launchd runs `<python> -P -m todo_system.engine_script
# kakao_token_refresh.sh` so the script resolves from the installed wheel, and
# the plist carries MACP_VAULT_ROOT since the script's own directory is then
# site-packages. Unset or empty, the plist is byte-identical to the pre-13d one.
# -P because WorkingDirectory is the vault, which may still carry a checkout's
# todo_system/.
ENGINE_PYTHON="${MACP_ENGINE_PYTHON:-}"
if [[ -n "$ENGINE_PYTHON" && "$ENGINE_PYTHON" != /* ]]; then
  echo "ERROR MACP_ENGINE_PYTHON must be an absolute path (launchd ProgramArguments): $ENGINE_PYTHON" >&2
  exit 2
fi
if [[ -n "$ENGINE_PYTHON" ]]; then
  PROGRAM_ARGUMENTS="    <string>${ENGINE_PYTHON}</string>
    <string>-P</string>
    <string>-m</string>
    <string>todo_system.engine_script</string>
    <string>kakao_token_refresh.sh</string>"
  VAULT_ROOT_PLIST_ENTRY="
    <key>MACP_VAULT_ROOT</key>
    <string>${VAULT_DIR}</string>"
else
  PROGRAM_ARGUMENTS="    <string>/bin/zsh</string>
    <string>${SCRIPT_PATH}</string>"
  VAULT_ROOT_PLIST_ENTRY=""
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

# Optional client_secret entry — only emitted if env var is set.
if [[ -n "${KAKAO_CLIENT_SECRET:-}" ]]; then
  KAKAO_CLIENT_SECRET_PLIST_ENTRY="    <key>KAKAO_CLIENT_SECRET</key>
    <string>${KAKAO_CLIENT_SECRET}</string>"
else
  KAKAO_CLIENT_SECRET_PLIST_ENTRY=""
fi

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
${PROGRAM_ARGUMENTS}
  </array>

  <key>WorkingDirectory</key>
  <string>${VAULT_DIR}</string>

  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>30</integer></dict>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>${VAULT_ROOT_PLIST_ENTRY}
    <key>KAKAO_REST_API_KEY</key>
    <string>${KAKAO_REST_API_KEY}</string>
${KAKAO_CLIENT_SECRET_PLIST_ENTRY}
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/kakao-refresh.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/kakao-refresh.err.log</string>
</dict>
</plist>
PLIST

chmod 600 "$PLIST_PATH"

if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" || true
fi

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "Installed: ${LABEL}"
echo "Schedule: every 4 hours (00:30, 04:30, 08:30, 12:30, 16:30, 20:30) + on login"
echo "Manual run: launchctl kickstart gui/\$(id -u)/${LABEL}"
