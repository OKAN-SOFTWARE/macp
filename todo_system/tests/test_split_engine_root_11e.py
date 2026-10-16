"""Split install: kakao_token_refresh / oauth_helper / notify_kakao data lives in the vault.

Notes/macp-public-split-inventory.md §11, leaf 11e (follow-up to 11d).

11d gave kakao_send.sh two anchors. The three other stand-alone kakao entry
points still used one: VAULT_DIR = KAKAO_VAULT_DIR or the parent of the script
directory. In a split install (engine checkout != vault, MACP_VAULT_ROOT names
the vault) refresh found no token, the oauth helper wrote a fresh token into
the engine tree, and notify_kakao.sh looked for the token and today's brief in
the engine. Each now resolves data (.secrets/, logs/, Todo/Briefings/,
.macp/config.toml) as KAKAO_VAULT_DIR > MACP_VAULT_ROOT > script parent, and
code (_build_brief_message.py, kakao_token_refresh.sh, todo_system) from the
engine checkout.

Every case runs a *copy* of the engine under tmp_path with a fake curl on PATH,
never the real checkout and never the network (same harness as 11d).
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest

from todo_system.tests.test_kakao_send import DEFAULT_LINK, SHELLS
from todo_system.tests.test_split_engine_root_11d import _Harness, _token, _vault_data

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_ENGINE_SCRIPTS = (
    "kakao_token_refresh.sh",
    "kakao_oauth_helper.sh",
    "notify_kakao.sh",
    "_build_brief_message.py",
)

pytestmark = pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(),
    reason="kakao scripts hardcode /usr/bin/python3",
)


def _engine_files(root: Path) -> Path:
    """scripts/ (the 11e kakao engine files) + todo_system/ at root."""
    (root / "scripts").mkdir(parents=True)
    for name in _ENGINE_SCRIPTS:
        shutil.copy2(_REAL_ENGINE / "scripts" / name, root / "scripts" / name)
    (root / "todo_system").symlink_to(_REAL_ENGINE / "todo_system")
    return root


def _brief(root: Path) -> Path:
    """Today's brief. notify_kakao.sh picks the file by `date +%F`; every notify
    call runs with TZ=UTC and both UTC today and tomorrow exist, so a run that
    straddles midnight still finds its file."""
    briefings = root / "Todo" / "Briefings"
    briefings.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date()
    body = "# Brief\n\n> split-11e-summary\n\n## 🎯 Top 3\n\n1. **alpha** — why\n\n## Next\n"
    for day in (today, today + dt.timedelta(days=1)):
        (briefings / f"{day.isoformat()}.md").write_text(body, encoding="utf-8")
    return root


def _log(root: Path) -> str:
    """refresh and notify share one log file."""
    path = root / "logs" / "notify_kakao.out.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _oauth_fields(h: _Harness, script: Path, *args: str, shell: str = "bash",
                  **env: str) -> dict[str, str]:
    r = h.send(script, *args, shell=shell, KAKAO_OAUTH_DRY_RUN="1",
               KAKAO_REST_API_KEY="TEST-KEY", **env)
    assert r.returncode == 0, r.stderr
    fields = {}
    for line in r.stdout.splitlines():
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


def _notify(h: _Harness, engine: Path, shell: str = "bash", **env: str):
    return h.send(engine / "scripts" / "notify_kakao.sh", shell=shell, TZ="UTC", **env)


def _template(h: _Harness, index: int = 0) -> dict:
    return json.loads(h.template_raw(index))


def _engine_clean(engine: Path) -> None:
    assert not (engine / ".secrets").exists()
    assert not (engine / "logs").exists()


@pytest.fixture
def split(tmp_path):
    """(harness, engine, vault): engine has no data, vault has no scripts/."""
    engine = _engine_files(tmp_path / "engine")
    vault = _vault_data(tmp_path / "vault")
    return _Harness(tmp_path), engine, vault


# --- kakao_oauth_helper.sh ---------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("profile", ["default", "cgv"])
def test_split_oauth_helper_writes_into_vault_root(split, shell, profile):
    h, engine, vault = split

    fields = _oauth_fields(h, engine / "scripts" / "kakao_oauth_helper.sh", profile,
                           shell=shell, MACP_VAULT_ROOT=str(vault))

    suffix = "" if profile == "default" else f"-{profile}"
    assert fields["token_file"] == str(vault / ".secrets" / f"kakao-token{suffix}.json")
    assert fields["app_file"] == str(vault / ".secrets" / f"kakao-app{suffix}.json")
    assert not (engine / ".secrets").exists()


# --- kakao_token_refresh.sh --------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
def test_split_refresh_loop_mode_rotates_vault_token(split, shell):
    h, engine, vault = split

    r = h.send(engine / "scripts" / "kakao_token_refresh.sh", shell=shell,
               MACP_VAULT_ROOT=str(vault))

    assert r.returncode == 0, r.stdout + r.stderr
    assert len(h.refresh_calls()) == 1
    assert any(a == "client_id=KEY-vault" for a in h.refresh_calls()[0])
    assert _token(vault)["access_token"] == "NEW-KEY-vault"
    assert _token(vault)["refresh_token"] == "RT-vault"
    assert "summary: 1 refreshed" in _log(vault)
    _engine_clean(engine)


def test_split_refresh_single_profile_mode(split):
    h, engine, vault = split

    r = h.send(engine / "scripts" / "kakao_token_refresh.sh",
               MACP_VAULT_ROOT=str(vault), KAKAO_PROFILE="default")

    assert r.returncode == 0, r.stdout + r.stderr
    assert len(h.refresh_calls()) == 1
    assert _token(vault)["access_token"] == "NEW-KEY-vault"
    assert "[default] refreshed access_token" in _log(vault)
    _engine_clean(engine)


def test_kakao_vault_dir_wins_for_refresh_and_oauth(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine_files(tmp_path / "engine")
    empty = _vault_data(tmp_path / "vault-a", token=False)
    chosen = _vault_data(tmp_path / "vault-b")
    env = {"MACP_VAULT_ROOT": str(empty), "KAKAO_VAULT_DIR": str(chosen)}

    r = h.send(engine / "scripts" / "kakao_token_refresh.sh", **env)
    fields = _oauth_fields(h, engine / "scripts" / "kakao_oauth_helper.sh", **env)

    assert r.returncode == 0, r.stdout + r.stderr
    assert _token(chosen)["access_token"] == "NEW-KEY-vault"
    assert fields["token_file"] == str(chosen / ".secrets" / "kakao-token.json")
    assert not (empty / "logs").exists()
    # _vault_data(token=False) still creates .secrets/, so check the file.
    assert not (empty / ".secrets" / "kakao-token.json").exists()


def test_combined_layout_refresh_and_oauth_resolve_to_script_parent(tmp_path):
    """Neither env set: data resolves to the script parent, as before the split."""
    h = _Harness(tmp_path)
    combined = _vault_data(_engine_files(tmp_path / "combined"))

    fields = _oauth_fields(h, combined / "scripts" / "kakao_oauth_helper.sh")
    r = h.send(combined / "scripts" / "kakao_token_refresh.sh")

    assert fields["token_file"] == str(combined / ".secrets" / "kakao-token.json")
    assert fields["app_file"] == str(combined / ".secrets" / "kakao-app.json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _token(combined)["access_token"] == "NEW-KEY-vault"
    assert "summary: 1 refreshed" in _log(combined)


# --- notify_kakao.sh ---------------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
def test_split_notify_reads_vault_brief_token_and_config(split, shell):
    h, engine, vault = split
    _brief(vault)

    r = _notify(h, engine, shell=shell, MACP_VAULT_ROOT=str(vault))

    assert r.returncode == 0, r.stdout + r.stderr
    assert h.bearers() == ["AT-vault"]
    template = _template(h)
    # _build_brief_message.py ran from the engine and read the vault brief.
    assert "split-11e-summary" in template["text"]
    assert "1. alpha" in template["text"]
    # Two-arg repo_slug: engine import, vault config. A one-arg regression
    # falls back to the neutral placeholder.
    assert template["link"]["web_url"] == DEFAULT_LINK
    assert "sent (http 200)" in _log(vault)
    _engine_clean(engine)


def test_split_notify_401_refreshes_vault_token(split):
    h, engine, vault = split
    _brief(vault)

    r = _notify(h, engine, MACP_VAULT_ROOT=str(vault), KAKAO_TEST_MEMO_CODES="401,200")

    assert r.returncode == 0, r.stdout + r.stderr
    assert len(h.refresh_calls()) == 1
    assert _token(vault)["access_token"] == "NEW-KEY-vault"
    assert _token(vault)["refresh_token"] == "RT-vault"
    assert h.bearers() == ["AT-vault", "NEW-KEY-vault"]
    assert "sent after refresh (http 200)" in _log(vault)
    _engine_clean(engine)


def test_kakao_vault_dir_wins_for_notify(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine_files(tmp_path / "engine")
    other = _vault_data(tmp_path / "vault-a", token=False)
    chosen = _brief(_vault_data(tmp_path / "vault-b"))

    r = _notify(h, engine, MACP_VAULT_ROOT=str(other), KAKAO_VAULT_DIR=str(chosen))

    assert r.returncode == 0, r.stdout + r.stderr
    assert h.bearers() == ["AT-vault"]
    assert "sent (http 200)" in _log(chosen)
    assert not (other / "logs").exists()


@pytest.mark.parametrize("codes", ["200", "401,200"])
def test_combined_layout_notify_resolves_to_script_parent(tmp_path, codes):
    """Neither env set: data and code both resolve to the script parent."""
    h = _Harness(tmp_path)
    combined = _brief(_vault_data(_engine_files(tmp_path / "combined")))

    r = _notify(h, combined, KAKAO_TEST_MEMO_CODES=codes)

    assert r.returncode == 0, r.stdout + r.stderr
    template = _template(h)
    assert template["link"] == {"web_url": DEFAULT_LINK, "mobile_web_url": DEFAULT_LINK}
    assert template["button_title"] == "Brief 열기"
    assert "split-11e-summary" in template["text"]
    if codes == "200":
        assert h.bearers() == ["AT-vault"]
        assert "sent (http 200)" in _log(combined)
    else:
        assert h.bearers() == ["AT-vault", "NEW-KEY-vault"]
        assert _token(combined)["access_token"] == "NEW-KEY-vault"
        assert "sent after refresh (http 200)" in _log(combined)
