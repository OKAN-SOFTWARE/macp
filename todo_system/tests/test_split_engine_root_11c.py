"""Split install (engine checkout != vault root): kakao_send.sh is an engine file.

Notes/macp-public-split-inventory.md §11, leaf 11c.

scripts/health_report.py (KAKAO_SEND_SH) and scripts/kakao_digest_send.py
(SEND_SH) built the sender path from VAULT. A split vault ships no scripts/, so
both constants pointed at a file that does not exist and every send failed.
They now resolve from the engine checkout (_INSTALL_DIR), like
scripts/claude_auth_watch.py. The vault-side constants stay on VAULT.

Only the constants are inspected: nothing is sent and no subprocess runs.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2]


def _vault_skeleton(root: Path) -> Path:
    """Marker + Todo/ + Memory/ + Worker/, and no engine files (no scripts/)."""
    (root / ".macp").mkdir(parents=True)
    (root / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (root / "Todo").mkdir()
    (root / "Memory").mkdir()
    (root / "Worker").mkdir()
    return root


def _load(monkeypatch, script: str, name: str):
    spec = importlib.util.spec_from_file_location(
        name, _ENGINE / "scripts" / f"{script}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Registration precedes exec (@dataclass resolves via sys.modules); a
    # unique name keeps the modules other test files registered untouched.
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def _under_engine(p: Path) -> bool:
    return _ENGINE.resolve() in p.resolve().parents


def test_health_report_kakao_send_sh_resolves_to_engine(tmp_path, monkeypatch):
    vault = _vault_skeleton(tmp_path / "vault")
    monkeypatch.delenv("KAKAO_VAULT_DIR", raising=False)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    H = _load(monkeypatch, "health_report", "health_report_11c_split")

    # Split layout sanity: the vault resolved to the tmp skeleton.
    assert Path(H.VAULT).resolve() == vault.resolve()
    assert not (vault / "scripts").exists()

    assert H.KAKAO_SEND_SH.exists(), f"sender not found: {H.KAKAO_SEND_SH}"
    assert _under_engine(H.KAKAO_SEND_SH)
    assert H.KAKAO_SEND_SH == H._INSTALL_DIR / "scripts" / "kakao_send.sh"

    # Vault-side constants stay on the vault.
    assert H.ASSIGNMENTS_DIR == H.VAULT / "Worker" / "assignments"
    assert H.STATE_PATH == H.VAULT / "Worker" / "_state" / "health.json"
    assert H.EPISODES_DIR == H.VAULT / "Memory" / "episodes"
    assert H.VAULT_LOGS_DIR == H.VAULT / "logs"

    # The combined-layout half below resolves vault_root() with no env, which needs a
    # .macp/config.toml marker at the engine root — present only when the engine is
    # installed inside a vault. An engine-only export tree has none (vault_root raises
    # by design), so skip the combined half there; the split-layout assertions above —
    # the actual split-11 deliverable — already ran (public-split-10 AC2 vault-only).
    if not (_ENGINE / ".macp" / "config.toml").is_file():
        pytest.skip("vault-only: combined layout (engine==vault) needs the engine "
                    "installed inside a vault")
    # Combined layout (engine == vault): same path as before the fix.
    monkeypatch.delenv("MACP_VAULT_ROOT")
    C = _load(monkeypatch, "health_report", "health_report_11c_combined")
    assert C.KAKAO_SEND_SH == C.VAULT / "scripts" / "kakao_send.sh"
    assert C.KAKAO_SEND_SH.exists()


def test_kakao_digest_send_send_sh_resolves_to_engine(tmp_path, monkeypatch):
    vault = _vault_skeleton(tmp_path / "vault")
    monkeypatch.delenv("KAKAO_VAULT_DIR", raising=False)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    K = _load(monkeypatch, "kakao_digest_send", "kakao_digest_send_11c_split")

    assert Path(K.VAULT).resolve() == vault.resolve()
    assert not (vault / "scripts").exists()

    assert K.SEND_SH.exists(), f"sender not found: {K.SEND_SH}"
    assert _under_engine(K.SEND_SH)
    assert K.SEND_SH == K._INSTALL_DIR / "scripts" / "kakao_send.sh"

    # Vault-side constants stay on the vault.
    assert K.QUEUE == vault / "logs" / "kakao_digest.jsonl"
    assert K.SENT == vault / "logs" / "kakao_digest.sent.jsonl"

    # The combined-layout half below resolves vault_root() with no env, which needs a
    # .macp/config.toml marker at the engine root — present only when the engine is
    # installed inside a vault. An engine-only export tree has none (vault_root raises
    # by design), so skip the combined half there; the split-layout assertions above —
    # the actual split-11 deliverable — already ran (public-split-10 AC2 vault-only).
    if not (_ENGINE / ".macp" / "config.toml").is_file():
        pytest.skip("vault-only: combined layout (engine==vault) needs the engine "
                    "installed inside a vault")
    # Combined layout (engine == vault): same path as before the fix.
    monkeypatch.delenv("MACP_VAULT_ROOT")
    C = _load(monkeypatch, "kakao_digest_send", "kakao_digest_send_11c_combined")
    assert C.SEND_SH == C.VAULT / "scripts" / "kakao_send.sh"
    assert C.SEND_SH.exists()
