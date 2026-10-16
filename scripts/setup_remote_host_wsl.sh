#!/usr/bin/env bash
set -uo pipefail

# setup_remote_host_wsl.sh — one-time setup for a remote worker host (M5)
#
# Prepares a Linux/WSL machine to run scripts/worker_remote_loop.sh:
#   1. installs uv (manages its own Python 3.12+, no sudo needed)
#   2. installs the claude CLI (native installer; npm fallback hint)
#   3. checks git push credentials for the vault remote
#   4. registers a cron entry (or prints the daemon alternative for WSL
#      distros where cron isn't running)
#
# Usage:
#   WORKER_HOST=windows scripts/setup_remote_host_wsl.sh

VAULT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER_HOST="${WORKER_HOST:-windows}"
# claude installs to ~/.local/bin (native installer); the old per-user claude bin
# dir is dangling post-migration. $CLAUDE_BIN_DIR optionally overrides.
export PATH="${CLAUDE_BIN_DIR:+$CLAUDE_BIN_DIR:}$HOME/.local/bin:$PATH"

echo "== M5 remote host setup: host=$WORKER_HOST vault=$VAULT_DIR =="

# 1) uv ----------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  echo "[1/4] uv: already installed ($(uv --version))"
else
  echo "[1/4] uv: installing to ~/.local/bin ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || { echo "uv install failed" >&2; exit 1; }
  export PATH="$HOME/.local/bin:$PATH"
  echo "      installed: $(uv --version)"
fi

# 2) claude CLI ---------------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  echo "[2/4] claude: already installed ($(claude --version 2>/dev/null | head -1))"
else
  echo "[2/4] claude: installing via native installer ..."
  if curl -fsSL https://claude.ai/install.sh | bash; then
    export PATH="${CLAUDE_BIN_DIR:+$CLAUDE_BIN_DIR:}$HOME/.local/bin:$PATH"
    echo "      installed: $(claude --version 2>/dev/null | head -1)"
  else
    echo "      native installer failed. Fallback (needs node >= 18):" >&2
    echo "        npm install -g @anthropic-ai/claude-code" >&2
    exit 1
  fi
fi
echo "      NOTE: run 'claude' once interactively to log in before the loop can work."

# 3) git credentials ----------------------------------------------------------
cd "$VAULT_DIR"
if git ls-remote origin >/dev/null 2>&1; then
  echo "[3/4] git: origin reachable ($(git remote get-url origin))"
else
  echo "[3/4] git: cannot reach origin — set up 'gh auth login' or an SSH key first" >&2
fi

# 4) scheduling ---------------------------------------------------------------
CRON_LINE="*/5 * * * * WORKER_HOST=$WORKER_HOST $VAULT_DIR/scripts/worker_remote_loop.sh >> $VAULT_DIR/logs/worker_remote.log 2>&1"
mkdir -p "$VAULT_DIR/logs"
if pgrep cron >/dev/null 2>&1 || service cron status >/dev/null 2>&1; then
  if crontab -l 2>/dev/null | grep -qF "worker_remote_loop.sh"; then
    echo "[4/4] cron: entry already present"
  else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "[4/4] cron: entry added (every 5 min)"
  fi
else
  echo "[4/4] cron is not running (common on WSL). Two options:"
  echo "  a) start cron on boot — add to /etc/wsl.conf:"
  echo "       [boot]"
  echo "       command=\"service cron start\""
  echo "     then add this crontab line:"
  echo "       $CRON_LINE"
  echo "  b) run the loop as a daemon (e.g. from Windows Task Scheduler at logon):"
  echo "       wsl.exe -d <distro> -- bash -lc 'WORKER_HOST=$WORKER_HOST $VAULT_DIR/scripts/worker_remote_loop.sh --daemon --interval 300 >> $VAULT_DIR/logs/worker_remote.log 2>&1'"
fi

echo "== setup done. Smoke test with:"
echo "   WORKER_HOST=$WORKER_HOST $VAULT_DIR/scripts/worker_remote_loop.sh"
