#!/bin/zsh
# Install LaunchAgent that runs the worker every 5 minutes.
# DOES NOT start aggressively — RunAtLoad is false; first execution waits until interval elapses.

set -euo pipefail

# Two anchors, as in worker_run_next.sh (11h). ENGINE_DIR is this script's own
# checkout (where worker_run_next.sh lives). VAULT_DIR is where the data lives
# (logs/, and the plist WorkingDirectory): MACP_VAULT_ROOT (split install) or
# the engine checkout itself (combined layout). The installer is run from
# inside its checkout, so the combined default resolves exactly as the old
# hardcoded fallback did.
# The plist also hands MACP_VAULT_ROOT=$VAULT_DIR to the daemon (11j), so the
# runner resolves the vault at run time instead of falling back to its own
# checkout. In the combined layout the value equals the runner's own
# ENGINE_DIR, so its split-only branch stays off (no-op).
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${MACP_VAULT_ROOT:-$ENGINE_DIR}"
LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.worker"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$VAULT_DIR/logs"
SCRIPT_PATH="$ENGINE_DIR/scripts/worker_run_next.sh"

# Installed mode (public split 13e, as 13d did for brief/orchestrator): with
# MACP_ENGINE_PYTHON set, launchd runs `<python> -P -m todo_system.engine_script
# worker_run_next.sh` so the script resolves from the installed wheel;
# engine_script hands it MACP_PYTHON, which is what worker_run_next.sh keys its
# installed branch on (13b). The env block below already carries
# MACP_VAULT_ROOT, so only ProgramArguments changes. Unset or empty, the plist
# is byte-identical to the pre-13e one. -P because WorkingDirectory is the
# vault, which may still carry a checkout's todo_system/.
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
    <string>worker_run_next.sh</string>"
else
  PROGRAM_ARGUMENTS="    <string>/bin/zsh</string>
    <string>${SCRIPT_PATH}</string>"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

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
  <integer>300</integer>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>MACP_VAULT_ROOT</key>
    <string>${VAULT_DIR}</string>
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/worker.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/worker.err.log</string>
</dict>
</plist>
PLIST

if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" || true
fi

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "Installed: ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Schedule: every 5 minutes (RunAtLoad=false)"
echo "Manual: launchctl kickstart gui/\$(id -u)/${LABEL}"
echo ""
echo "WARNING: each worker run invokes 'claude -p' up to 5 times (one per stage)."
echo "         Make sure your subscription/budget can handle the load before enabling."
echo ""
echo "To disable: launchctl bootout gui/\$(id -u) ${PLIST_PATH}"
