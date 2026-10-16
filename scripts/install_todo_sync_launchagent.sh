#!/bin/zsh
set -euo pipefail

# Two anchors, as in install_worker/orchestrator/brief_launchagent.sh
# (11i/11k/11l). ENGINE_DIR is this script's own checkout (the todo_system
# package). VAULT_DIR is where the data lives (logs/, .macp/config.toml, the
# plist WorkingDirectory — the daemon's DB paths are cwd-relative):
# MACP_VAULT_ROOT (split install) or the engine checkout itself (combined
# layout). The installer is run from inside its checkout, so the combined
# default resolves exactly as the old hardcoded fallback did.
# The plist runs `uv run --project $ENGINE_DIR` (after `run`, as in
# orchestrator_tick.sh) and hands MACP_VAULT_ROOT=$VAULT_DIR to the daemon
# (11m). todo_system is not installed (package = false), so --project alone
# would still import whatever todo_system sits in the vault cwd: a split
# install also puts the engine on PYTHONPATH and runs python -P. The combined
# layout emits neither, so its only changes are --project and the env value.
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${MACP_VAULT_ROOT:-$ENGINE_DIR}"
UV_BIN="${MACP_UV_BIN:-$HOME/.local/bin/uv}"
LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.todo-sync"
# Calendar id: env override → this vault's .macp/config.toml (stdlib tomllib) →
# empty. When empty the plist omits --calendar-id and the CLI's own default applies,
# so the engine installer bakes in no personal calendar id (public-split leaf 7).
CALENDAR_ID="${MACP_CALENDAR_ID:-}"
if [ -z "$CALENDAR_ID" ] && [ -f "$VAULT_DIR/.macp/config.toml" ]; then
  CALENDAR_ID="$(python3 -c 'import sys,pathlib,tomllib; d=tomllib.loads((pathlib.Path(sys.argv[1])/".macp"/"config.toml").read_text()); v=d.get("calendar_id"); print(v if isinstance(v,str) else "")' "$VAULT_DIR" 2>/dev/null || true)"
fi
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$VAULT_DIR/logs"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$LOG_DIR"

# Emit the --calendar-id argument pair only when we have a value; an empty one would
# make the daemon sync to a blank calendar instead of falling back to the CLI default.
if [ -n "$CALENDAR_ID" ]; then
  CAL_ARGS="    <string>--calendar-id</string>
    <string>${CALENDAR_ID}</string>
"
else
  CAL_ARGS=""
fi

# Split install only: import the engine's todo_system, never a copy in the
# vault cwd (see the anchor comment above). Combined: both fragments empty.
if [[ "$VAULT_DIR" != "$ENGINE_DIR" ]]; then
  PY_ISOLATE="    <string>-P</string>
"
  PYTHONPATH_ENV="    <key>PYTHONPATH</key>
    <string>${ENGINE_DIR}</string>
"
else
  PY_ISOLATE=""
  PYTHONPATH_ENV=""
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
    <string>${UV_BIN}</string>
    <string>run</string>
    <string>--project</string>
    <string>${ENGINE_DIR}</string>
    <string>python</string>
${PY_ISOLATE}    <string>-u</string>
    <string>-m</string>
    <string>todo_system.cli</string>
    <string>daemon</string>
    <string>--interval</string>
    <string>3</string>
    <string>--calendar-sync</string>
${CAL_ARGS}  </array>

  <key>WorkingDirectory</key>
  <string>${VAULT_DIR}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>MACP_VAULT_ROOT</key>
    <string>${VAULT_DIR}</string>
${PYTHONPATH_ENV}  </dict>

  <key>RunAtLoad</key>
  <true/>

  <!-- Run every 5 min, NOT KeepAlive. In DB mode cmd_daemon does one
       scan_queue then exits; KeepAlive respawned it every ~10s, and each
       scan fetches the full orchestrator_runs table — that hammered the
       Supabase egress quota (402 exceed_egress_quota). -->
  <key>StartInterval</key>
  <integer>300</integer>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/todo-sync.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/todo-sync.err.log</string>
</dict>
</plist>
PLIST

if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" || true
fi

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo "Installed and started: ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Logs: ${LOG_DIR}/todo-sync.out.log, ${LOG_DIR}/todo-sync.err.log"
