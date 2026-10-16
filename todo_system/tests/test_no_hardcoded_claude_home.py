"""Ratchet: the legacy single Claude config dir must not be spelled out anywhere
the account layout module (``todo_system/macp/accounts.py``) does not own.

Leaf ③ of ``macp-multi-claude-account`` moved every runtime consumer to the
active account (``macp.config.current_account``); the files that still spell the
old single dir are listed in ``PENDING_CONVERSION`` and pinned **both ways**: a
hit in a file that is not listed fails (a new hard-coding crept in), and a listed
file without a hit fails too (it was converted — drop it from the list). Leaf ④
removes what it converts; leaf ⑤ ends with ``frozenset()``.

Scan: ``todo_system/**/*.py``, ``scripts/**/*.py``, ``scripts/**/*.sh``. Excluded:
``todo_system/tests/**`` (this file and its siblings may spell the literal),
``todo_system/macp/accounts.py`` (the two ``LEGACY_*`` constants) and every line
whose first non-blank character is ``#`` — comment *lines*; a trailing comment on
a code line still counts. The patterns are given in prose in the task's
acceptance criteria; ``HIT_EXAMPLES`` / ``MISS_EXAMPLES`` self-check them.

No literal from the identifier gate's PATTERN appears in this file: it sits
inside the scanned engine tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN = (("todo_system", "*.py"), ("scripts", "*.py"), ("scripts", "*.sh"))
EXCLUDED_FILES = frozenset({"todo_system/macp/accounts.py"})
EXCLUDED_PREFIX = "todo_system/tests/"

PATTERNS = (
    # (a) ~ / $HOME / ${HOME} followed by /.claude and then NOT a word char or '-'
    #     (so ~/.claude-shared and ~/.claude-accounts/dev are not hits; ~/.claude.json is)
    re.compile(r"(?:~|\$HOME|\$\{HOME\})/\.claude(?![\w-])"),
    # (b) <something>.home() / '.claude' or ".claude.json" — any spacing around the slash
    re.compile(r"home\(\)\s*/\s*[\x27\x22]\.claude(?:\.json)?[\x27\x22]"),
    # (c) expanduser( opening a quoted literal that starts with ~/.claude — with the
    #     same lookahead as (a), so expanduser("~/.claude-accounts") stays consistent
    re.compile(r"expanduser\([\x27\x22]~/\.claude(?![\w-])"),
)

HIT_EXAMPLES = (
    "~/.claude",
    "$HOME/.claude/hooks",
    "${HOME}/.claude",
    "Path.home() / '.claude'",
    'Path.home() / ".claude.json"',
    "Path.home()/'.claude'",
    "os.path.expanduser('~/.claude/automation-token')",
    'os.path.expanduser("~/.claude")',
    "~/.claude.json",
    "x = foo()  # ~/.claude in a trailing comment still counts",
)
MISS_EXAMPLES = (
    "~/.claude-shared",
    "~/.claude-accounts/dev",
    "$HOME/.claude-accounts",
    ".claude/commands",
    "MACP_CLAUDE_SHARED_ROOT",
    "Path.home() / '.claude-shared'",
    "expanduser('~/.claude-accounts/dev')",
    "# ~/.claude in a comment line",
    "    # indented comment: Path.home() / '.claude'",
    "LEGACY_CONFIG_DIR",
)

# Files still spelling the legacy single config dir (repo-relative, posix).
# ④ removes what it converts; ⑤ ends with frozenset().
PENDING_CONVERSION = frozenset()   # leaf ⑤: every runtime consumer converted; scan() must be empty

# The public engine export tree has no identifier-gate script (it is self-excluded)
# and lacks the vault-only PENDING files (scripts/public_export_exclude.txt). Same
# signal as test_public_gates_real_tree: there, a listed file that is simply absent
# is not a conversion — only unexpected hits and present-but-clean files fail.
_EXPORT_TREE = not (REPO / "scripts" / "check_public_identifiers.sh").is_file()


def _is_hit(line: str) -> bool:
    if line.lstrip().startswith("#"):
        return False
    return any(p.search(line) for p in PATTERNS)


def _scan_files():
    for sub, pattern in SCAN:
        root = REPO / sub
        if not root.is_dir():
            continue
        for path in sorted(root.rglob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith(EXCLUDED_PREFIX) or rel in EXCLUDED_FILES:
                continue
            yield rel, path


def scan() -> dict[str, list[int]]:
    """``{repo-relative path: [1-based line numbers]}`` of every hit."""
    hits: dict[str, list[int]] = {}
    for rel, path in _scan_files():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        where = [i for i, line in enumerate(lines, 1) if _is_hit(line)]
        if where:
            hits[rel] = where
    return hits


def test_patterns_self_check():
    for s in HIT_EXAMPLES:
        assert _is_hit(s), s
    for s in MISS_EXAMPLES:
        assert not _is_hit(s), s


def test_scan_covers_the_three_trees():
    rels = {rel for rel, _ in _scan_files()}
    assert "todo_system/macp/accounts.py" not in rels
    assert not any(r.startswith(EXCLUDED_PREFIX) for r in rels)
    assert any(r.startswith("todo_system/") and r.endswith(".py") for r in rels)
    assert any(r.startswith("scripts/") and r.endswith(".py") for r in rels)
    assert any(r.startswith("scripts/") and r.endswith(".sh") for r in rels)
    # the consumers leaf ③ converted are scanned — and clean (test_hits_match_pending_exactly)
    for must in ("todo_system/claude_auth.py", "todo_system/usage_guard.py",
                 "todo_system/orchestrator/runner.py", "todo_system/orchestrator/stages.py",
                 "todo_system/orchestrator/model_select.py", "todo_system/macp/config.py",
                 "todo_system/macp/__main__.py", "scripts/macp_dashboard.py"):
        assert must in rels, must


def test_hits_match_pending_exactly():
    hits = scan()
    unexpected = set(hits) - PENDING_CONVERSION
    assert not unexpected, {p: hits[p] for p in sorted(unexpected)}
    missing = PENDING_CONVERSION - set(hits)
    if _EXPORT_TREE:
        missing = {p for p in missing if (REPO / p).exists()}
    assert not missing, sorted(missing)
