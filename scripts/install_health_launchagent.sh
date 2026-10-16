#!/bin/zsh
# Install a LaunchAgent that runs the weekly health report
# (scripts/health_report.py) every Sunday 08:30 — the meta-watchdog that checks
# whether the automation's safety nets themselves are still alive.
#
# 정상이면 무음: the report always lands in Memory/episodes/YYYY-MM-DD-health.md,
# kakao fires only when a check WARNs. The default invocation does NOT git-commit
# the episode (avoids index.lock races with the orchestrator tick) — commit it
# manually or run health_report.py --commit yourself.
#
# StartCalendarInterval: a sleeping Mac runs the job on next wake; a powered-off
# Mac skips that week. RunAtLoad=false — installing never triggers an immediate run.
#
# DRY_RUN=1 (or --dry-run): write the plist to a temp dir + plutil -lint only,
# no launchctl — catches XML typos that would otherwise fail silently inside
# launchd.

set -euo pipefail

VAULT_DIR="${MACP_VAULT_ROOT:-$HOME/Projects/MaestroACP}"
LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.health"
LOG_DIR="$VAULT_DIR/logs"
SCRIPT_PATH="$VAULT_DIR/scripts/health_report.py"

# Installed mode (public split 13d, as gen_units does in 13c): with
# MACP_ENGINE_PYTHON set, launchd runs `<python> -P -m todo_system.engine_script
# health_report.py` (engine_script runs it with that same venv python, not
# /usr/bin/python3), and the plist carries MACP_VAULT_ROOT since the script's
# own directory is then site-packages. Unset or empty, the plist is
# byte-identical to the pre-13d one. -P because WorkingDirectory is the vault,
# which may still carry a checkout's todo_system/.
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
    <string>health_report.py</string>"
  VAULT_ROOT_PLIST_ENTRY="
    <key>MACP_VAULT_ROOT</key>
    <string>${VAULT_DIR}</string>"
else
  PROGRAM_ARGUMENTS="    <string>/usr/bin/python3</string>
    <string>${SCRIPT_PATH}</string>"
  VAULT_ROOT_PLIST_ENTRY=""
fi

DRY_RUN="${DRY_RUN:-0}"
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

if [[ "$DRY_RUN" == "1" ]]; then
  PLIST_PATH="$(mktemp -d)/${LABEL}.plist"
else
  PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
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
  <dict>
    <key>Weekday</key>
    <integer>0</integer>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>${VAULT_ROOT_PLIST_ENTRY}
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/health.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/health.err.log</string>
</dict>
</plist>
PLIST

plutil -lint "$PLIST_PATH"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN: plist written to $PLIST_PATH (launchctl skipped)"
  exit 0
fi

if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" || true
fi

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "Installed: ${LABEL} (weekly, Sunday 08:30, RunAtLoad=false)"
echo "To disable: launchctl bootout gui/\$(id -u) ${PLIST_PATH}"
