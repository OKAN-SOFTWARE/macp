"""Render test for scripts/claude_auth_watch.py's kakao text composer.

scripts/ has no __init__.py, so the module is loaded by path via importlib.
`_compose_kakao_text` is pure (no subprocess), so this never reaches the real
kakao channel ([[tests-reach-real-alert-sender]]). The auth alert's whole reason
to exist is the remedy ("터미널에서 claude 실행 후 /login") — the old
`f"🔐 {title}\\n{message}"[:179]` could blind-cut it; this pins that it now fits.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from todo_system.kakao_format import KAKAO_TEXT_LIMIT

VAULT = Path(__file__).resolve().parents[2]
_SRC = VAULT / "scripts" / "claude_auth_watch.py"


def _load():
    spec = importlib.util.spec_from_file_location("claude_auth_watch", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load()


def test_auth_alert_keeps_the_login_remedy_within_budget():
    title = "Claude Code 인증 이상"
    message = ("refreshTokenExpiresAt가 지났고 accessToken이 비었습니다 — "
               "터미널에서 claude 실행 후 /login")
    text = A._compose_kakao_text(title, message)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert text.splitlines()[0].startswith("🔐 Claude Code 인증 이상")
    assert "/login" in text                     # the remedy survives (short input, whole)


def test_auth_alert_worst_input_stays_bounded():
    text = A._compose_kakao_text("인증 이상", "problem detail " * 40)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert text.splitlines()[0].startswith("🔐 인증 이상")
