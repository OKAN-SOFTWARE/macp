"""Unit tests for ``todo_system.claude_auth`` — headless auth fallback rules.

`claude_auth.py` had zero dedicated tests despite gating whether every
headless `claude -p` stage authenticates correctly. Every case here drives
the ``path=``/``base=``/``env=`` seams against a throwaway ``tmp_path`` or an
explicit dict, so none of them touch the real ``~/.claude/automation-token``
file or the process ``os.environ`` — results must not depend on the host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from todo_system.claude_auth import (
    MalformedToken,
    _looks_like_token,
    auth_source,
    claude_env,
    read_automation_token,
)

# 40+ chars, alnum + hyphen only — passes _looks_like_token's shape check.
DUMMY_TOKEN = "sk-ant-oat01-" + "a" * 40


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "token"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# read_automation_token — normal / absent paths
# ---------------------------------------------------------------------------


def test_read_returns_stripped_token(tmp_path):
    p = _write(tmp_path, f"  {DUMMY_TOKEN}\n")
    assert read_automation_token(path=p) == DUMMY_TOKEN


def test_read_missing_file_returns_none(tmp_path):
    assert read_automation_token(path=tmp_path / "missing") is None


def test_read_empty_file_returns_none(tmp_path):
    p = _write(tmp_path, "")
    assert read_automation_token(path=p) is None


def test_read_whitespace_only_file_returns_none(tmp_path):
    p = _write(tmp_path, "   \n\t\n")
    assert read_automation_token(path=p) is None


# ---------------------------------------------------------------------------
# _looks_like_token / strict rejection
# ---------------------------------------------------------------------------


def test_looks_like_token_accepts_dummy():
    assert _looks_like_token(DUMMY_TOKEN) is True


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "a" * 20 + " " + "a" * 20,  # internal whitespace, 40+ chars
        "pbpaste > ~/.claude/automation-token" + "a" * 10,  # shell input
        "sk-ant-oat01-" + "a" * 30 + "/",  # no whitespace, but a forbidden char
    ],
    ids=["too-short", "internal-whitespace", "shell-command", "path-like-no-space"],
)
def test_malformed_content_raises_when_strict(tmp_path, bad):
    p = _write(tmp_path, bad)
    with pytest.raises(MalformedToken) as exc_info:
        read_automation_token(path=p, strict=True)
    assert bad not in str(exc_info.value)
    assert str(len(bad)) in str(exc_info.value)


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "a" * 20 + " " + "a" * 20,
        "pbpaste > ~/.claude/automation-token" + "a" * 10,
        "sk-ant-oat01-" + "a" * 30 + "/",
    ],
    ids=["too-short", "internal-whitespace", "shell-command", "path-like-no-space"],
)
def test_malformed_content_returns_none_when_not_strict(tmp_path, bad):
    p = _write(tmp_path, bad)
    assert read_automation_token(path=p, strict=False) is None


# ---------------------------------------------------------------------------
# claude_env
# ---------------------------------------------------------------------------


def test_claude_env_leaves_api_key_untouched(tmp_path):
    p = _write(tmp_path, DUMMY_TOKEN)
    base = {"ANTHROPIC_API_KEY": "x"}
    env = claude_env(base=base, path=p)
    assert env == {"ANTHROPIC_API_KEY": "x"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_claude_env_injects_valid_token(tmp_path):
    p = _write(tmp_path, DUMMY_TOKEN)
    env = claude_env(base={}, path=p)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == DUMMY_TOKEN


def test_claude_env_skips_malformed_token_without_raising(tmp_path):
    p = _write(tmp_path, "short")
    env = claude_env(base={}, path=p)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_claude_env_does_not_mutate_base(tmp_path):
    p = _write(tmp_path, DUMMY_TOKEN)
    base = {"FOO": "bar"}
    base_before = dict(base)
    claude_env(base=base, path=p)
    assert base == base_before


# ---------------------------------------------------------------------------
# auth_source
# ---------------------------------------------------------------------------


def test_auth_source_api_key():
    assert auth_source(env={"ANTHROPIC_API_KEY": "x"}) == "api-key"


def test_auth_source_automation_token():
    assert auth_source(env={"CLAUDE_CODE_OAUTH_TOKEN": "x"}) == "automation-token"


def test_auth_source_keychain_fallback():
    assert auth_source(env={}) == "keychain-oauth"


def test_auth_source_api_key_wins_over_automation_token():
    env = {"ANTHROPIC_API_KEY": "x", "CLAUDE_CODE_OAUTH_TOKEN": "y"}
    assert auth_source(env=env) == "api-key"
