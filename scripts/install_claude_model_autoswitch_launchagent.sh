#!/bin/zsh
# Install a LaunchAgent that keeps ~/.claude/settings.json pointed at a model
# that can actually run (scripts/claude_model_autoswitch.py), every 30 minutes.
#
# Why this exists: when the selected model runs out of credits, interactive
# Claude Code sessions keep using it and fail on every turn until a human
# notices and runs /model. The worker half of this recovery is automatic (a
# stage that prints a model-unavailable notice drops the 24h model_pref.json
# cache, so the next dispatch re-probes); this agent is the interactive half.
#
# Each tick probes and, in the common case, changes nothing and prints nothing
# — an idle system leaves no daemon-log noise. It writes only when the
# configured model is dead, replacing exactly one key with a backup
# (settings.json.bak) plus an atomic temp-file swap. Every other key in
# settings.json is preserved, and a settings.json that does not parse is left
# strictly untouched.
#
# The script itself is git-tracked (scripts/claude_model_autoswitch.py), so a
# fresh clone has it. Only its *state*
# (Worker/_state/claude_model_autoswitch.json) stays git-ignored. The existence
# check below therefore guards against a wrong VAULT_DIR / partial checkout,
# not a missing local-only file.
#
# DRY_RUN=1 (or --dry-run): write the plist to a temp dir + plutil -lint only,
# no launchctl — catches XML typos that would otherwise fail silently in launchd.

set -euo pipefail

VAULT_DIR="${MACP_VAULT_ROOT:-$HOME/Projects/MaestroACP}"
LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.claude-model-autoswitch"
LOG_DIR="$VAULT_DIR/logs"
SCRIPT_PATH="$VAULT_DIR/scripts/claude_model_autoswitch.py"
INTERVAL=1800

# Installed mode (public split 13e, as 13d did for health): with
# MACP_ENGINE_PYTHON set, launchd runs `<python> -P -m todo_system.engine_script
# claude_model_autoswitch.py` (engine_script runs it with that same venv python,
# not /usr/bin/python3), and the plist carries MACP_VAULT_ROOT since the
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
    <string>claude_model_autoswitch.py</string>"
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

# Default mode only: installed mode resolves the script from the wheel, so the
# vault need not carry scripts/ at all.
if [[ -z "$ENGINE_PYTHON" && ! -f "$SCRIPT_PATH" ]]; then
  echo "claude_model_autoswitch.py not found at $SCRIPT_PATH" >&2
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
  <string>${LOG_DIR}/claude-model-autoswitch.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/claude-model-autoswitch.err.log</string>
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
echo "Tick log: $LOG_DIR/claude-model-autoswitch.out.log (quiet unless it switches)"
echo "To disable: launchctl bootout gui/\$(id -u) ${PLIST_PATH}"
