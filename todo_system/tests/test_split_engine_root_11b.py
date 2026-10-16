"""Split install (engine checkout != vault root): two more places mixed the roots.

Notes/macp-public-split-inventory.md §11, leaf 11b.

1. worker.cmd_run_parallel() spawned `python -m todo_system.worker run-next`
   with cwd=ROOT (the vault) and nothing else, so cwd alone had to serve as
   both the import path and the vault anchor. In a split install the vault has
   no todo_system/ to import and cwd-resolving modules can land elsewhere. The
   child now gets the parent's vault as MACP_VAULT_ROOT and the engine at the
   front of PYTHONPATH — only when the roots differ; the combined layout keeps
   env=None, byte-identical to before.

2. scripts/health_report.py:gather_code_time() read the commit time of
   todo_system/ and scripts/ from the vault's git. A split vault repo holds
   neither path, git answers empty, and judge_daemon_staleness sat at "skip"
   forever. It now reads the engine checkout (_INSTALL_DIR).

No real child process is started: subprocess.Popen is swapped for a recorder.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import worker

_ENGINE = Path(__file__).resolve().parents[2]
_HEALTH_SRC = _ENGINE / "scripts" / "health_report.py"


def _vault_skeleton(root: Path) -> Path:
    """Marker + Todo/ + Worker/, and no engine files (no todo_system/, scripts/)."""
    (root / ".macp").mkdir(parents=True)
    (root / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (root / "Todo").mkdir()
    (root / "Worker").mkdir()
    return root


class _FakeProc:
    pid = 4242

    def wait(self):
        return 0


@pytest.mark.parametrize(
    "case", ["split-no-pythonpath", "split-prior-pythonpath", "combined"]
)
def test_run_parallel_child_gets_vault_and_engine_import_path(
    tmp_path, monkeypatch, case
):
    vault = _vault_skeleton(tmp_path / "vault")
    prior = tmp_path / "prior"
    monkeypatch.setattr(worker, "ROOT", vault)
    monkeypatch.setattr(worker, "WORKER_DIR", vault / "Worker")
    monkeypatch.setattr(worker, "queue_lock", contextlib.nullcontext)
    monkeypatch.setattr(
        worker, "load_queue", lambda *a, **k: {"runs": [{"status": "pending"}]}
    )
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    monkeypatch.setenv("MACP_11B_SENTINEL", "1")
    if case == "split-prior-pythonpath":
        monkeypatch.setenv("PYTHONPATH", str(prior))
    else:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    if case == "combined":
        monkeypatch.setattr(worker, "_ENGINE_ROOT", vault.resolve())

    calls: list[tuple[list[str], dict]] = []

    def fake_popen(args, **kwargs):
        calls.append((list(args), kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    rc = worker.cmd_run_parallel(1, dry_run=True)

    assert rc == 0
    spawned = [(a, k) for a, k in calls if "run-next" in a]
    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert args[1:] == ["-m", "todo_system.worker", "run-next", "--dry-run"]
    assert kwargs["cwd"] == vault

    if case == "combined":
        # Combined layout: the child inherits exactly what it did before.
        assert kwargs.get("env") is None
        return

    assert worker._ENGINE_ROOT != vault
    env = kwargs.get("env")
    assert env is not None, "run-parallel child spawned without an explicit env"
    assert env["MACP_VAULT_ROOT"] == str(vault)
    assert env["MACP_11B_SENTINEL"] == "1"
    if case == "split-no-pythonpath":
        assert env["PYTHONPATH"] == str(worker._ENGINE_ROOT)
    else:
        assert env["PYTHONPATH"].split(os.pathsep) == [
            str(worker._ENGINE_ROOT),
            str(prior),
        ]


def _load_health_report(monkeypatch, name: str):
    spec = importlib.util.spec_from_file_location(name, _HEALTH_SRC)
    mod = importlib.util.module_from_spec(spec)
    # Registration precedes exec (@dataclass resolves via sys.modules); a
    # unique name keeps test_health_report's module untouched.
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def test_gather_code_time_reads_engine_checkout_not_vault(tmp_path, monkeypatch):
    vault = _vault_skeleton(tmp_path / "vault")
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    H = _load_health_report(monkeypatch, "health_report_11b")

    # Split layout sanity: the vault resolved to the tmp skeleton.
    assert Path(H.VAULT).resolve() == vault.resolve()
    assert H._INSTALL_DIR != H.VAULT
    ct = H.gather_code_time()
    assert ct is not None, "code time read from the vault, which ships no engine code"
    assert H.judge_daemon_staleness({}, ct).level != "skip"

    # Combined layout: same value as the git log of the (shared) checkout.
    monkeypatch.setattr(H, "VAULT", H._INSTALL_DIR)
    out = subprocess.run(
        ["git", "-C", str(H._INSTALL_DIR), "log", "-1", "--format=%cI", "--",
         *H.CODE_TIME_PATHS],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0 and out.stdout.strip()
    expected = dt.datetime.fromisoformat(out.stdout.strip())
    assert H.gather_code_time() == expected

    # A real git failure still yields skip.
    assert H.judge_daemon_staleness({}, None).level == "skip"
