"""Project-local memory — lives in `<project>/.macp/memory/`.

Development memory for a specific project lives WITH the project (git-tracked under
`.macp/memory/`), not in the central control-plane vault — so you push it and get
the exact same working context in another environment.

It also plugs into Claude Code's native context loading: `macp init` makes the
project's `CLAUDE.md` (or `AGENTS.md`) `@`-import `.macp/memory/index.md`, so BOTH
a plain Claude Code session AND the MaestroACP pipeline auto-load the accumulated
project memory (Claude Code resolves `@path` imports in its memory files).
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import PROJECT_DIR

MEMORY_IMPORT = "@.macp/memory/index.md"
CLAUDE_CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md")   # Claude Code auto-loaded


def memory_dir(root: Path | str) -> Path:
    return Path(root) / PROJECT_DIR / "memory"


def index_path(root: Path | str) -> Path:
    return memory_dir(root) / "index.md"


def ensure_memory(root: Path | str) -> Path:
    d = memory_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    idx = index_path(root)
    if not idx.exists():
        idx.write_text(
            "# Project memory (MaestroACP)\n\n"
            "이 프로젝트를 개발할 때 필요한 컨텍스트·결정·사실을 여기 누적한다. "
            "`.macp/memory/` 파일들은 프로젝트 repo와 함께 커밋/푸시되어 다른 환경에서도 "
            "동일한 컨텍스트로 이어진다.\n", encoding="utf-8")
    return idx


def discover_context(root: Path | str) -> dict:
    """What Claude-Code-visible context this project has (for status / injection)."""
    root = Path(root)
    md = memory_dir(root)
    return {
        "memory_dir": str(md) if md.exists() else None,
        "memory_files": sorted(p.name for p in md.glob("*.md")) if md.exists() else [],
        "claude_md": str(root / "CLAUDE.md") if (root / "CLAUDE.md").exists() else None,
        "agents_md": str(root / "AGENTS.md") if (root / "AGENTS.md").exists() else None,
        "claude_dir": str(root / ".claude") if (root / ".claude").is_dir() else None,
    }


POINTER = ("MaestroACP project memory: read `.macp/memory/` for accumulated "
           "project context (decisions, facts, conventions).")


def link_agent_context(root: Path | str) -> list[str]:
    """Wire the project memory into the agent context files so **any** vendor's
    agent picks it up:
      - CLAUDE.md  → `@.macp/memory/index.md` (Claude Code resolves @-imports).
      - AGENTS.md  → a plain pointer (the vendor-neutral standard read by Codex,
        Cursor, Aider, Gemini, …; they read it as text, not @-imports).
    If neither exists, create the vendor-neutral AGENTS.md (+ a CLAUDE.md import).
    Returns the actions taken."""
    ensure_memory(root)
    root = Path(root)
    actions: list[str] = []
    cm, am = root / "CLAUDE.md", root / "AGENTS.md"

    # Claude Code: native @-import in CLAUDE.md (only if the project uses one)
    if cm.exists():
        t = cm.read_text(encoding="utf-8")
        if MEMORY_IMPORT not in t:
            sep = "" if (not t or t.endswith("\n")) else "\n"
            cm.write_text(t + f"{sep}\n<!-- MaestroACP -->\n{MEMORY_IMPORT}\n", encoding="utf-8")
            actions.append("CLAUDE.md @import")
    # Vendor-neutral AGENTS.md: ALWAYS ensure a pointer (read by Codex/Cursor/Aider/
    # Gemini/… as plain text) — this is what makes other companies' agents work.
    if am.exists():
        t = am.read_text(encoding="utf-8")
        if ".macp/memory" not in t:
            sep = "" if (not t or t.endswith("\n")) else "\n"
            am.write_text(t + f"{sep}\n<!-- MaestroACP -->\n{POINTER}\n", encoding="utf-8")
            actions.append("AGENTS.md pointer")
    else:
        guide = (
            f"# Agent guide\n\n<!-- MaestroACP -->\n{POINTER}\n\n"
            "이 프로젝트는 MaestroACP(`macp`)로 관리됩니다. 작업하며 의미있는 결정·컨텍스트·"
            "gotcha가 생기면 `macp memory add <name> --content \"...\"`로 `.macp/memory/`에 "
            "남기세요 — 프로젝트 repo와 함께 커밋되어 다른 환경/에이전트가 그대로 이어받습니다. "
            "(등록/진행상황 업데이트는 `/macp` 슬래시 커맨드로도 가능.)\n")
        am.write_text(guide, encoding="utf-8")
        actions.append("AGENTS.md created")
    return actions


_ITEMS_HEADER = "## 항목"
_DEFAULT_PRELUDE = "# Project memory (MaestroACP)\n\n프로젝트 개발 컨텍스트·결정·사실 누적.\n"
# A top-level item line: `- [text](target)<tail>`. Indented lines are continuations.
_LINK_RE = re.compile(r"^-\s*\[[^\]]*\]\(([^)\s]+)\)(.*)$")


def _parse_index(text: str) -> tuple[str, list[str], dict[str, tuple[str, list[str]]], str]:
    """Split an existing index.md into (prelude, head_lines, items, trailer).

    prelude = everything before the `## 항목` line (the whole text if absent);
    head_lines = non-link lines in the section before its first item;
    items = {target: (tail after `](target)`, continuation lines)} — first wins;
    trailer = from the first `#` header after the section to the end."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == _ITEMS_HEADER), None)
    if start is None:
        return text, [], {}, ""
    head: list[str] = []
    items: dict[str, tuple[str, list[str]]] = {}
    trailer = ""
    current: list[str] | None = head
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.startswith("#"):
            trailer = "\n".join(lines[j:])
            break
        m = _LINK_RE.match(line)
        if m:
            key = m.group(1).removeprefix("./")
            if key in items:
                current = None          # duplicate: drop it and its continuation lines
            else:
                items[key] = (m.group(2), [])
                current = items[key][1]
        elif line.strip() and current is not None:
            current.append(line)
    return "\n".join(lines[:start]), head, items, trailer


def _refresh_index(root: Path | str) -> None:
    """Regenerate index.md: the file list and its order come from the directory,
    while each item's hand-written tail, its continuation lines and all prose
    outside the `## 항목` section are carried over from the existing index."""
    d = memory_dir(root)
    idx = index_path(root)
    files = sorted(p.name for p in d.glob("*.md") if p.name != "index.md")
    existing = idx.read_text(encoding="utf-8") if idx.exists() else ""
    prelude, head, items, trailer = _parse_index(existing)
    if not prelude.strip():
        prelude = _DEFAULT_PRELUDE
    out = prelude.rstrip("\n") + "\n"
    if files:
        out += "\n" + _ITEMS_HEADER + "\n" + "".join(f"{line}\n" for line in head)
        for f in files:
            tail, cont = items.get(f, ("", []))
            out += f"- [{f}]({f}){tail}\n" + "".join(f"{line}\n" for line in cont)
    if trailer.strip("\n"):
        out += "\n" + trailer.strip("\n") + "\n"
    idx.write_text(out, encoding="utf-8")


def write_memory(root: Path | str, name: str, content: str) -> Path:
    ensure_memory(root)
    p = memory_dir(root) / f"{name}.md"
    p.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    _refresh_index(root)
    return p


def read_memory(root: Path | str, limit: int = 8000) -> str:
    """Concatenate project memory for prompt injection (bounded)."""
    d = memory_dir(root)
    if not d.exists():
        return ""
    parts = []
    for p in sorted(d.glob("*.md")):
        if p.name == "index.md":
            continue
        parts.append(f"### {p.name}\n{p.read_text(encoding='utf-8')}")
    text = "\n\n".join(parts)
    return text[:limit]
