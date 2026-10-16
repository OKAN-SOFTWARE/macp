#!/bin/zsh
# Install a LaunchAgent that keeps a per-agent orchestrator Runner standing by.
#
# Usage:  scripts/install_runner_launchagent.sh <agent-id> [shadow|live]
#   e.g.  scripts/install_runner_launchagent.sh armchair-commander shadow
#
# Unlike the worker (interval-based), a runner is a long-lived daemon: KeepAlive
# restarts it if it dies, RunAtLoad starts it now. Default mode is `shadow` (no
# Claude Code is spawned) — safe to run while a human develops the same project.
# Switch to `live` only when you want the runner to actually execute tasks.

set -euo pipefail

AGENT="${1:?usage: install_runner_launchagent.sh <agent-id> [shadow|live|live-remote|live-staged] [mailbox-uri]}"
MODE="${2:-shadow}"
URI="${3:-git://Worker/agents}"

VAULT_DIR="${MACP_VAULT_ROOT:-$HOME/Projects/MaestroACP}"
LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.runner.${AGENT}"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$VAULT_DIR/logs"

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
    <string>/bin/zsh</string>
    <string>-lc</string>
    <!-- PATH is re-prepended HERE, not only in EnvironmentVariables below: this is
         a login shell (-lc), so /etc/zprofile runs macOS path_helper, which rebuilds
         PATH from /etc/paths (first entry: /usr/local/bin) and appends the inherited
         value after it. That resurrects the nodejs.org node v16.17.0 from 2022-08 and
         shadows node@22 no matter what the plist says — verified 2026-08-18. -->
    <string>export PATH=/opt/homebrew/opt/node@22/bin:\$PATH; cd ${VAULT_DIR} &amp;&amp; exec uv run python -m todo_system.orchestrator.runner --agent ${AGENT} --mode ${MODE} --uri ${URI} --daemon</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${VAULT_DIR}</string>

  <key>KeepAlive</key>
  <true/>

  <key>RunAtLoad</key>
  <true/>

  <key>EnvironmentVariables</key>
  <dict>
    <!-- openjdk is keg-only, so it is not on the default PATH and /usr/bin/java is
         only a stub that reports "Unable to locate a Java Runtime". Without this a
         runner cannot build any Gradle/Android/KMP project — which matters now that
         verify can fall back to local checks when CI is unavailable.

         node@22 is prepended for the same reason, and it must come BEFORE
         /usr/local/bin: that path holds a nodejs.org node v16.17.0 from 2022-08
         which shadows everything. advote (pnpm workspaces, engines.node ">=20")
         could not be built at all on 2026-08-18 — its verify parked at
         FAIL(unverified) and 6 TS files reached a PR having never been compiled.
         node@22 is keg-only too, so /opt/homebrew/bin alone is not enough.
         pnpm comes from node@22's corepack (0.34), not a separate install. -->
    <key>PATH</key>
    <string>/opt/homebrew/opt/openjdk@17/bin:/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>JAVA_HOME</key>
    <string>/opt/homebrew/opt/openjdk@17</string>
    <key>ANDROID_HOME</key>
    <string>${HOME}/Library/Android/sdk</string>
    <!-- StandardOutPath is a file, so Python block-buffers stdout at 8 KB and the
         log trails reality by however long it takes to fill that. On 2026-08-05
         the runner had been dispatching all day while its log still showed the
         previous day — exactly the wrong thing when you are diagnosing a stall.
         Free here: this process logs a handful of lines per tick. -->
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/runner.${AGENT}.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/runner.${AGENT}.err.log</string>
</dict>
</plist>
PLIST

if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" || true
fi

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "Installed: ${LABEL} (mode=${MODE})"
echo "Plist: ${PLIST_PATH}"
if [[ "$MODE" == "live" ]]; then
  echo ""
  echo "WARNING: live mode spawns 'claude -p' in an isolated worktree per TASK_ASSIGN."
  echo "         Make sure your subscription/budget can handle it, and that no human is"
  echo "         hand-developing the same project (use shadow first)."
fi
echo ""
echo "To disable: launchctl bootout gui/\$(id -u) ${PLIST_PATH}"
