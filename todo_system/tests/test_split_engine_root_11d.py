"""Split install (engine checkout != vault root): kakao_send.sh data lives in the vault.

Notes/macp-public-split-inventory.md §11, leaf 11d (follow-up to 11c).

kakao_send.sh used one variable, VAULT_DIR = KAKAO_VAULT_DIR or the parent of
the script directory, both for its data (.secrets/, logs/, .macp/config.toml)
and for its sibling engine scripts (kakao_route.py, kakao_token_refresh.sh).
In a split install MACP_VAULT_ROOT names the vault, but the script ignored it:
it looked for tokens under the engine checkout, found none and skipped the
send. It now keeps two anchors: VAULT_DIR = KAKAO_VAULT_DIR > MACP_VAULT_ROOT >
script parent for data, ENGINE_DIR = script parent for code.

Every case runs a *copy* of the engine (scripts/ files copied, todo_system/
symlinked) under tmp_path, never the real checkout. A regression in the anchor
logic would otherwise read, rewrite (401 self-heal) or log into the real
.secrets/ and logs/ of whichever checkout runs the suite. The copy is made from
the real files at test time, so it always tests the current bytes. A fake curl
on PATH keeps every case off the network (same harness as test_kakao_send.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from todo_system.tests.test_kakao_send import (
    DEFAULT_LINK,
    FAKE_CURL,
    SHELLS,
    TEST_REPO_SLUG,
    _urlencoded_field,
)

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_ENGINE_SCRIPTS = ("kakao_send.sh", "kakao_token_refresh.sh", "kakao_route.py")

pytestmark = pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(),
    reason="kakao scripts hardcode /usr/bin/python3",
)


def _engine_files(root: Path) -> Path:
    """scripts/ (the three kakao engine files) + todo_system/ at root."""
    (root / "scripts").mkdir(parents=True)
    for name in _ENGINE_SCRIPTS:
        shutil.copy2(_REAL_ENGINE / "scripts" / name, root / "scripts" / name)
    (root / "todo_system").symlink_to(_REAL_ENGINE / "todo_system")
    return root


def _vault_data(root: Path, *, token: bool = True) -> Path:
    """Marker config, default token + app, and no engine files."""
    (root / ".macp").mkdir(parents=True, exist_ok=True)
    (root / ".macp" / "config.toml").write_text(
        f'repo_slug = "{TEST_REPO_SLUG}"\n', encoding="utf-8")
    secrets = root / ".secrets"
    secrets.mkdir(exist_ok=True)
    if token:
        (secrets / "kakao-token.json").write_text(json.dumps(
            {"access_token": "AT-vault", "refresh_token": "RT-vault"}), encoding="utf-8")
        (secrets / "kakao-app.json").write_text(json.dumps(
            {"rest_api_key": "KEY-vault"}), encoding="utf-8")
    return root


class _Harness:
    """Fake curl + a from-scratch env. KAKAO_VAULT_DIR is absent unless passed."""

    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.curl_log = tmp / "curl_argv.jsonl"
        self.home.mkdir()
        self.bin.mkdir()
        self.curl_log.touch()
        curl = self.bin / "curl"
        curl.write_text(FAKE_CURL, encoding="utf-8")
        curl.chmod(0o755)

    def send(self, script: Path, *args: str, shell: str = "bash", **env_extra: str):
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "KAKAO_TEST_CURL_LOG": str(self.curl_log),
            "KAKAO_SEND_STRICT": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            **env_extra,
        }
        # errors="replace": see KakaoVault.run in test_kakao_send.py.
        return subprocess.run(
            [shell, str(script), *args],
            env=env, capture_output=True, text=True, errors="replace",
            timeout=120, check=False,
        )

    def calls(self) -> list[list[str]]:
        text = self.curl_log.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def memo_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if any("talk/memo/default/send" in a for a in c)]

    def refresh_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if any("kauth.kakao.com/oauth/token" in a for a in c)]

    def bearers(self) -> list[str]:
        out = []
        for call in self.memo_calls():
            for i, arg in enumerate(call):
                if arg == "-H" and i + 1 < len(call) and call[i + 1].startswith("Authorization:"):
                    out.append(call[i + 1].split("Bearer ", 1)[-1])
        return out

    def template_raw(self, index: int = 0) -> str:
        return _urlencoded_field(self.memo_calls()[index], "template_object")


def _log(root: Path) -> str:
    path = root / "logs" / "kakao_send.out.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _token(root: Path) -> dict:
    return json.loads((root / ".secrets" / "kakao-token.json").read_text(encoding="utf-8"))


@pytest.fixture
def split(tmp_path):
    """(harness, engine, vault): engine has no data, vault has no scripts/."""
    engine = _engine_files(tmp_path / "engine")
    vault = _vault_data(tmp_path / "vault")
    return _Harness(tmp_path), engine, vault


@pytest.mark.parametrize("shell", SHELLS)
def test_split_reads_token_and_logs_from_vault_root(split, shell):
    h, engine, vault = split
    marker = "split-11d-marker"

    r = h.send(engine / "scripts" / "kakao_send.sh", marker, shell=shell,
               MACP_VAULT_ROOT=str(vault))

    assert r.returncode == 0, r.stderr
    assert h.bearers() == ["AT-vault"]
    assert "sent (200)" in _log(vault) and marker in _log(vault)
    # repo_slug came from the vault config, not the engine checkout.
    assert json.loads(h.template_raw())["link"]["web_url"] == DEFAULT_LINK
    # Nothing landed in the engine tree.
    assert not (engine / ".secrets").exists()
    assert not (engine / "logs").exists()


def test_split_401_self_heal_refreshes_vault_token(split):
    h, engine, vault = split

    r = h.send(engine / "scripts" / "kakao_send.sh", "hello",
               MACP_VAULT_ROOT=str(vault), KAKAO_TEST_MEMO_CODES="401,200")

    assert r.returncode == 0, r.stderr
    assert len(h.refresh_calls()) == 1
    assert _token(vault)["access_token"] == "NEW-KEY-vault"
    assert _token(vault)["refresh_token"] == "RT-vault"
    assert h.bearers() == ["AT-vault", "NEW-KEY-vault"]
    assert "sent after refresh (200)" in _log(vault)
    assert not (engine / ".secrets").exists()
    assert not (engine / "logs").exists()


def test_split_route_runs_from_engine(split):
    """kakao_route.py is an engine file; looked up in the vault it is missing and
    a silent alert would go out as an immediate send."""
    h, engine, vault = split

    r = h.send(engine / "scripts" / "kakao_send.sh", "quiet",
               MACP_VAULT_ROOT=str(vault), KAKAO_LEVEL="silent")

    assert r.returncode == 0, r.stderr
    assert h.memo_calls() == []
    assert "route: SILENT" in _log(vault)
    # Routing state follows the data anchor too.
    assert (vault / "logs" / "kakao_route.log").exists()
    assert not (engine / "logs").exists()


def test_kakao_vault_dir_still_wins_over_macp_vault_root(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine_files(tmp_path / "engine")
    empty = _vault_data(tmp_path / "vault-a", token=False)
    chosen = _vault_data(tmp_path / "vault-b")

    r = h.send(engine / "scripts" / "kakao_send.sh", "hello",
               MACP_VAULT_ROOT=str(empty), KAKAO_VAULT_DIR=str(chosen))

    assert r.returncode == 0, r.stderr
    assert h.bearers() == ["AT-vault"]
    assert "sent (200)" in _log(chosen)
    assert not (empty / "logs").exists()


@pytest.mark.parametrize("codes", ["200", "401,200"])
def test_combined_layout_resolves_to_script_parent(tmp_path, codes):
    """Neither env set: data and code both resolve to the script parent, exactly
    as before the split. The payload is byte-identical to the default golden."""
    h = _Harness(tmp_path)
    combined = _vault_data(_engine_files(tmp_path / "combined"))

    r = h.send(combined / "scripts" / "kakao_send.sh", "hello", KAKAO_TEST_MEMO_CODES=codes)

    assert r.returncode == 0, r.stderr
    expected = {
        "object_type": "text",
        "text": "hello",
        "link": {"web_url": DEFAULT_LINK, "mobile_web_url": DEFAULT_LINK},
        "button_title": "열기",
    }
    assert h.template_raw() == json.dumps(expected, ensure_ascii=False)
    if codes == "200":
        assert h.bearers() == ["AT-vault"]
        assert "sent (200)" in _log(combined)
    else:
        assert h.bearers() == ["AT-vault", "NEW-KEY-vault"]
        assert _token(combined)["access_token"] == "NEW-KEY-vault"
        assert "sent after refresh (200)" in _log(combined)
