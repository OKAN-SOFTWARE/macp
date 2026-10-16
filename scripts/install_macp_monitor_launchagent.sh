#!/bin/zsh
# Install a LaunchAgent that runs the macp usage monitor
# (scripts/macp_monitor.py) every 30 minutes.
#
# The monitor is a *sensor*: it never files issues. Each run writes the newest
# report to Worker/_state/macp_monitor/latest.json and prints to the daemon log
# ONLY when there are new candidates — so an idle system leaves no noise, and a
# session can read latest.json to see what the sensor has been seeing while
# nobody was watching. Filing (`gh issue create` + `mark-filed`) stays a
# human/session judgment call, per the monitor's design.
#
# Why this exists: the monitor previously ran only inside an interactive /loop,
# so detection stopped the moment the session ended.
#
# The monitor script itself is git-tracked (scripts/macp_monitor.py), so a fresh
# clone has it. Only its *state* (Worker/_state/macp_monitor/: log cursors, the
# latest report, filed-issue markers) stays git-ignored, so the vault auto-sync
# daemons never touch it. The existence check below therefore guards against a
# wrong VAULT_DIR / partial checkout, not a missing local-only file.
#
# DRY_RUN=1 (or --dry-run): write the plist to a temp dir + plutil -lint only,
# no launchctl — catches XML typos that would otherwise fail silently in launchd.

set -euo pipefail

VAULT_DIR="${MACP_VAULT_ROOT:-$HOME/Projects/MaestroACP}"
LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.macp-monitor"
LOG_DIR="$VAULT_DIR/logs"
SCRIPT_PATH="$VAULT_DIR/scripts/macp_monitor.py"
INTERVAL=1800

# Installed mode (public split 13e, as 13d did for health): with
# MACP_ENGINE_PYTHON set, launchd runs `<python> -P -m todo_system.engine_script
# macp_monitor.py scan --daemon` (engine_script runs it with that same venv
# python, not /usr/bin/python3), and the plist carries MACP_VAULT_ROOT since the
# script's own directory is then site-packages. Unset or empty, the plist is
# byte-identical to the pre-13e one. -P because WorkingDirectory is the vault,
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
    <string>macp_monitor.py</string>
    <string>scan</string>
    <string>--daemon</string>"
  VAULT_ROOT_PLIST_ENTRY="
    <key>MACP_VAULT_ROOT</key>
    <string>${VAULT_DIR}</string>"
else
  PROGRAM_ARGUMENTS="    <string>/usr/bin/python3</string>
    <string>${SCRIPT_PATH}</string>
    <string>scan</string>
    <string>--daemon</string>"
  VAULT_ROOT_PLIST_ENTRY=""
fi

DRY_RUN="${DRY_RUN:-0}"
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

# Default mode only: installed mode resolves the script from the wheel, so the
# vault need not carry scripts/ at all.
if [[ -z "$ENGINE_PYTHON" && ! -f "$SCRIPT_PATH" ]]; then
  echo "macp_monitor.py not found at $SCRIPT_PATH" >&2
  echo "It is tracked in git — check VAULT_DIR and that the checkout is complete." >&2
  exit 1
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

  <key>StartInterval</key>
  <integer>${INTERVAL}</integer>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>${VAULT_ROOT_PLIST_ENTRY}
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/macp-monitor.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/macp-monitor.err.log</string>
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

echo "Installed: ${LABEL} (every ${INTERVAL}s, RunAtLoad=false)"
echo "Latest report: $VAULT_DIR/Worker/_state/macp_monitor/latest.json"
echo "To disable: launchctl bootout gui/\$(id -u) ${PLIST_PATH}"
