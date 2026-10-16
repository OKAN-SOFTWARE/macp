"""Dispatch Claude Code *background agents* so an AI runner's session runs
autonomously AND stays observable/controllable from claude.ai/code, the mobile
app (Code tab), and the terminal (`claude attach|logs|stop <id>`).

Verified 2026-07-20 against the installed CLI:

  claude --bg "<prompt>" --worktree <name> --permission-mode <mode>

  * The prompt is POSITIONAL — `--bg` conflicts with `-p/--print` (print mode
    never starts the attachable session `claude agents` hooks into).
  * `--worktree <name>` isolates the run in <repo>/.claude/worktrees/<name>.
  * Dispatch returns immediately, printing `backgrounded · <id>`.
  * Sessions show in `claude agents --json` with kind="background"; the local
    `claude remote-control` server surfaces them to claude.ai/code.

Stdlib-only (subprocess/json/re).
"""

from __future__ import annotations

import json
import re
import subprocess

CLAUDE = "claude"
WATCH_URL = "https://claude.ai/code"

# dispatch prints e.g. "backgrounded · c8bad7c5"
_BACKGROUNDED = re.compile(r"backgrounded\s*[·:]\s*([0-9a-fA-F]{6,})")


def build_dispatch_argv(prompt: str, worktree: str | None = None,
                        permission_mode: str | None = None) -> list[str]:
    argv = [CLAUDE, "--bg", prompt]
    if worktree:
        argv += ["--worktree", worktree]
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    return argv


def parse_session_id(stdout: str) -> str | None:
    m = _BACKGROUNDED.search(stdout or "")
    return m.group(1) if m else None


def dispatch(prompt: str, cwd, worktree: str | None = None,
             permission_mode: str = "acceptEdits") -> tuple[str | None, subprocess.CompletedProcess]:
    """Dispatch a background agent; return (session_id_prefix, completed_proc)."""
    r = subprocess.run(
        build_dispatch_argv(prompt, worktree, permission_mode),
        cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    return parse_session_id(r.stdout), r


def list_sessions(include_completed: bool = False) -> list[dict]:
    args = [CLAUDE, "agents", "--json"] + (["--all"] if include_completed else [])
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def session_active(session_id: str) -> bool:
    """True while the (short-prefix) session id is still in the active list."""
    return any(str(s.get("sessionId", "")).startswith(session_id)
               for s in list_sessions())


def watch_pointer(session_id: str) -> dict:
    """How the human watches/controls this session — from phone/web or terminal."""
    return {
        "session_id": session_id,
        "watch_url": WATCH_URL,
        "attach": f"claude attach {session_id}",
        "logs": f"claude logs {session_id}",
        "stop": f"claude stop {session_id}",
    }
