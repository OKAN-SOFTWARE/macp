#!/usr/bin/env bash
set -uo pipefail

# worker_remote_loop.sh — remote-host worker loop (M5)
#
# Runs on a secondary machine (e.g. the Windows/WSL desktop). Pulls the vault,
# executes tasks from this host's git-tracked inbox
# (Worker/remote/$WORKER_HOST/inbox/*.md), then pushes results (queue.json,
# run artifacts, Memory/episodes) back so the primary host can review them.
#
# Usage:
#   WORKER_HOST=windows scripts/worker_remote_loop.sh            # one pass
#   WORKER_HOST=windows scripts/worker_remote_loop.sh --daemon   # loop forever
#   WORKER_HOST=windows scripts/worker_remote_loop.sh --daemon --interval 300
#
# Requirements on this machine: git (authenticated for push), uv, claude CLI.

VAULT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER_HOST="${WORKER_HOST:-}"
INTERVAL=300
DAEMON=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --daemon) DAEMON=1; shift ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$WORKER_HOST" || "$WORKER_HOST" == "default" ]]; then
  echo "error: WORKER_HOST must be set to this machine's host name (e.g. windows)" >&2
  exit 2
fi
export WORKER_HOST

# Common tool locations that cron/Task Scheduler shells miss. claude lives in
# ~/.local/bin (native installer) or nvm; the old per-user claude bin dir is
# dangling after the config-dir migration. $CLAUDE_BIN_DIR optionally overrides.
export PATH="${CLAUDE_BIN_DIR:+$CLAUDE_BIN_DIR:}$HOME/.local/bin:/usr/local/bin:$PATH"
# claude installed via npm/nvm: load nvm only if claude isn't found yet.
if ! command -v claude >/dev/null 2>&1 && [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.nvm/nvm.sh" >/dev/null 2>&1 || true
fi

for tool in git uv claude; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: '$tool' not found on PATH — run scripts/setup_remote_host_wsl.sh first" >&2
    exit 2
  fi
done

HOST_DIR="$VAULT_DIR/Worker/remote/$WORKER_HOST"
LOG_PREFIX() { date "+%FT%T"; }

sync_pull() {
  cd "$VAULT_DIR" || return 1
  git checkout main -q 2>/dev/null
  # -c rebase.autoStash: the `--autostash` pull flag needs git >= 2.27; the
  # config form works on older gits (e.g. Ubuntu 18.04's 2.17).
  if ! git -c rebase.autoStash=true pull --rebase origin main; then
    echo "[$(LOG_PREFIX)] pull --rebase failed; aborting rebase and skipping pass" >&2
    git rebase --abort 2>/dev/null
    return 1
  fi
  return 0
}

sync_push() {
  cd "$VAULT_DIR" || return 1
  git checkout main -q 2>/dev/null
  git add "Worker/remote/$WORKER_HOST" Memory/episodes 2>/dev/null
  if git diff --cached --quiet; then
    return 0  # nothing to push
  fi
  git commit -q -m "chore(worker): sync results from host $WORKER_HOST" || return 1
  local attempt
  for attempt in 1 2 3; do
    if git push origin main; then
      return 0
    fi
    echo "[$(LOG_PREFIX)] push failed (attempt $attempt) — rebasing and retrying" >&2
    git -c rebase.autoStash=true pull --rebase origin main || git rebase --abort 2>/dev/null
    sleep 5
  done
  echo "[$(LOG_PREFIX)] push failed after 3 attempts — results stay committed locally" >&2
  return 1
}

one_pass() {
  echo "[$(LOG_PREFIX)] pass start (host=$WORKER_HOST)"
  sync_pull || return 1
  cd "$VAULT_DIR" || return 1
  uv run python -m todo_system.worker scan-queue
  # run-next exit codes: 0 done/idle, 1 failed stage, 2 dirty-abort — the
  # queue file records the outcome either way; always try to push results.
  uv run python -m todo_system.worker run-next
  local rc=$?
  sync_push
  echo "[$(LOG_PREFIX)] pass end (run-next rc=$rc)"
  return 0
}

if [[ "$DAEMON" == "1" ]]; then
  echo "[$(LOG_PREFIX)] daemon mode: host=$WORKER_HOST interval=${INTERVAL}s vault=$VAULT_DIR"
  while true; do
    one_pass || true
    sleep "$INTERVAL"
  done
else
  one_pass
fi
