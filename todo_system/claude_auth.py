"""헤드리스 `claude` 호출용 인증 환경.

왜 필요한가: Keychain의 OAuth 리프레시 토큰은 **절대 만료**를 갖는다(갱신으로
연장되지 않는다). 만료되면 토큰이 빈 문자열로 비워지고 헤드리스 `claude -p`가
전멸하는데, 증상이 없어서 며칠 뒤에야 발견된다 — 2026-08-15에 실제로 겪었고,
2026-09-07 재로그인은 만료를 90일이 아니라 19일만 밀었다(9/15 → 10/4).

해법: `claude setup-token`으로 발급한 장수 bearer 토큰을
``<config dir>/automation-token`` (600)에 두고 `CLAUDE_CODE_OAUTH_TOKEN`으로 주입한다.
구독 기반이라 API 과금이 붙지 않는다. 설계 근거: scripts/REMOTE_CONTROL_SETUP.md
(이 규약은 claude_remote_control.sh가 이미 쓰던 것으로, 여기서 재사용한다).

config dir은 **활성 Claude 계정**의 것이다(`macp.config.current_account`: 프로젝트
`.macp` pin → 프로필 `active_account` → priority). 계정이 없으면 종전 단일 dir
(`macp.accounts.LEGACY_CONFIG_DIR`; 모듈 상수 ``TOKEN_FILE``이 그 파일이고 호환용으로
남는다, 실제 해석은 :func:`token_file`)이다. 계정이 해석되면 :func:`claude_env` 는
`accounts.claude_env_for` 로 config dir 층과 토큰 층을 함께 적용하고, 그 계정 dir에
토큰 파일이 없으면 상속받은 `CLAUDE_CODE_OAUTH_TOKEN`을 **제거**한다(다른 계정의 토큰이
새지 않게).

**계정이 없을 때 파일이 없으면 아무것도 하지 않는다** — 즉 미발급 상태에서도 동작이
변하지 않고, 발급하면 자동으로 적용되며, 문제가 생기면 파일만 지우면 즉시 롤백된다.
"""

from __future__ import annotations

import os
from pathlib import Path

from .macp import accounts as _accounts

# The legacy single-dir token file, fixed at import time as before (the
# monkeypatch seam). Kept under this name for compatibility; :func:`token_file`
# is the resolver callers go through.
TOKEN_FILE = _accounts.legacy_config_dir() / "automation-token"


class MalformedToken(ValueError):
    """토큰 파일 내용이 bearer 토큰 모양이 아니다."""


def _looks_like_token(s: str) -> bool:
    """bearer 토큰의 최소 형태 검사.

    2026-09-07에 `pbpaste > <토큰 파일>` 을 실행했더니 클립보드에
    setup-token 출력이 아니라 **그 명령줄 자체**가 들어갔다. 파일이 비어있지
    않다는 이유로 그대로 주입했다면 모든 헤드리스 스테이지가 인증 실패로 죽었을
    것이다 — 잘못된 토큰은 '토큰 없음'보다 나쁘다(없으면 Keychain으로 폴백된다).
    그래서 모양이 아니면 주입하지 않고 시끄럽게 실패한다.
    """
    if not s or len(s) < 40:
        return False
    if any(c.isspace() for c in s):   # 토큰에 공백/개행은 없다
        return False
    # 셸 명령·경로·URL이 섞여 들어온 경우
    if any(bad in s for bad in ("&&", "|", " ", "/", "~", "$", "<", ">")):
        return False
    return True


def _current_account():
    """The active claude account (``macp.config.current_account`` from the cwd),
    or None. Never raises — a half-installed vault must not break auth."""
    try:
        from .macp.config import current_account
        return current_account()[0]
    except Exception:
        return None


def token_file() -> Path:
    """Where the automation token lives: ``<active account dir>/automation-token``,
    or the module-level :data:`TOKEN_FILE` (the legacy single dir — and the
    monkeypatch seam) when no account is configured."""
    acct = _current_account()
    return TOKEN_FILE if acct is None else _accounts.token_file_for(acct)


def read_automation_token(path: Path | None = None, *, strict: bool = True) -> str | None:
    """토큰 문자열, 없거나 비었으면 None. 토큰 내용은 절대 로깅하지 않는다.

    ``path`` 를 주지 않으면 :func:`token_file` (활성 계정 dir, 계정이 없으면
    ``TOKEN_FILE``). ``strict`` 이면 모양이 틀린 내용에 대해 :class:`MalformedToken`
    을 올린다.
    """
    p = path or token_file()
    try:
        token = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None
    if not _looks_like_token(token):
        if strict:
            raise MalformedToken(
                f"{p} 내용이 bearer 토큰 형태가 아닙니다 (길이 {len(token)}). "
                "`claude setup-token` 출력만 넣어야 합니다 — 파일을 지우면 "
                "Keychain 인증으로 폴백합니다."
            )
        return None
    return token


def claude_env(base: dict | None = None, path: Path | None = None) -> dict:
    """`claude` 서브프로세스에 넘길 환경.

    ``path`` 를 주지 않으면 **활성 계정**을 따른다(:func:`_current_account`): 계정이
    있으면 ``macp.accounts.claude_env_for`` 가 config dir 층(계정 dir이 LEGACY가 아닐
    때만 ``CLAUDE_CONFIG_DIR``)과 토큰 층(계정 dir의 토큰 주입, 없으면 상속받은
    ``CLAUDE_CODE_OAUTH_TOKEN`` 제거)을 적용한다. 계정이 없거나 ``path`` 를 명시하면
    종전 그대로 — 토큰이 있으면 주입, 없으면 env 를 **그대로** 둔다(제거 없음: 전역
    env 토큰에 기대는 무계정 호스트가 깨지지 않게).

    ``ANTHROPIC_API_KEY``가 이미 설정돼 있으면 토큰 층은 건드리지 않는다 — 그 경우
    사용자가 의도적으로 API 과금 경로를 쓰는 것이고, 여기서 덮으면 조용히
    청구 주체가 바뀐다.
    """
    env = dict(os.environ if base is None else base)
    if path is None:
        acct = _current_account()
        if acct is not None:
            return _accounts.claude_env_for(acct, env)
    if env.get("ANTHROPIC_API_KEY"):
        return env
    # 모양이 틀린 토큰은 주입하지 않는다 — 주입하면 전 스테이지가 인증 실패로
    # 죽고, 안 하면 Keychain 으로 조용히 폴백된다. 후자가 안전하다.
    # (틀렸다는 사실은 claude_auth_watch 가 별도로 시끄럽게 알린다.)
    token = read_automation_token(path, strict=False)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def auth_source(env: dict | None = None) -> str:
    """지금 어떤 자격증명으로 도는지 — 로그·헬스리포트용 한 단어."""
    e = os.environ if env is None else env
    if e.get("ANTHROPIC_API_KEY"):
        return "api-key"
    if e.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "automation-token"
    return "keychain-oauth"
