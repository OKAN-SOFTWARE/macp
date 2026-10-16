"""Installed-wheel seams in worker.cmd_run_parallel and beta_publish (13a, P3).

Leaf 11b taught ``cmd_run_parallel`` to hand a split install's child the vault
(``MACP_VAULT_ROOT``) and the engine (front of ``PYTHONPATH``). For an
installed wheel the second half is wrong: ``_ENGINE_ROOT`` is
``site-packages/todo_system/_engine``, so the entry would point at
site-packages, and the package is importable from the child's site-packages
anyway. The child now gets the vault only when ``paths.is_installed()``; the
split-checkout and combined cases keep the 11b contract byte for byte.

``beta_publish`` kept its per-install scratch (lock dir, sweep state/log/
worktrees) under the engine root, which an installed wheel cannot write. The
scratch root is layout-conditional: engine root in a checkout (the existing
pins hold — immune to a redirected vault), the vault's ``Worker/_state`` in a
wheel. ``_spawn_detached`` starts the sweep in the vault-data root, so a child
with no env resolves the vault by the cwd-upward search; in the combined
layout that is the same directory as before.

The 11b recorder pattern is copied here on purpose (``todo_system/tests`` is
not a package, so sibling test modules are not imported). No real child
process is started. No literal from the identifier gate's pattern may appear
in this file: it sits inside the scanned tree.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import paths, worker
from todo_system.orchestrator import beta_publish

ENGINE = Path(__file__).resolve().parents[2]


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


def _run_parallel_env(tmp_path, monkeypatch, *, installed: bool, prior: Path | None):
    vault = _vault_skeleton(tmp_path / "vault")
    engine = tmp_path / "site" / "todo_system" / "_engine" if installed else tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    monkeypatch.setattr(worker, "ROOT", vault)
    monkeypatch.setattr(worker, "WORKER_DIR", vault / "Worker")
    monkeypatch.setattr(worker, "_ENGINE_ROOT", engine.resolve())
    monkeypatch.setattr(worker, "queue_lock", contextlib.nullcontext)
    monkeypatch.setattr(worker, "load_queue", lambda *a, **k: {"runs": [{"status": "pending"}]})
    monkeypatch.setattr(paths, "is_installed", lambda package_dir=None: installed)
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    monkeypatch.setenv("MACP_13A_SENTINEL", "1")
    if prior is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", str(prior))

    calls: list[tuple[list[str], dict]] = []

    def fake_popen(args, **kwargs):
        calls.append((list(args), kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert worker.cmd_run_parallel(1, dry_run=True) == 0
    spawned = [(a, k) for a, k in calls if "run-next" in a]
    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert args[1:] == ["-m", "todo_system.worker", "run-next", "--dry-run"]
    assert kwargs["cwd"] == vault
    return vault, engine.resolve(), kwargs.get("env")


# --- cmd_run_parallel: installed wheel → vault only, no PYTHONPATH --------------


def test_run_parallel_installed_child_gets_vault_but_no_pythonpath(tmp_path, monkeypatch):
    vault, engine, env = _run_parallel_env(tmp_path, monkeypatch, installed=True, prior=None)
    assert engine.parts[-2:] == ("todo_system", "_engine")
    assert env is not None, "installed split child spawned without an explicit env"
    assert env["MACP_VAULT_ROOT"] == str(vault)
    assert env["MACP_13A_SENTINEL"] == "1"
    assert "PYTHONPATH" not in env
    assert all(str(engine) not in v for v in env.values())


def test_run_parallel_installed_child_keeps_prior_pythonpath_untouched(tmp_path, monkeypatch):
    prior = tmp_path / "prior"
    vault, engine, env = _run_parallel_env(tmp_path, monkeypatch, installed=True, prior=prior)
    assert env["MACP_VAULT_ROOT"] == str(vault)
    # The parent's PYTHONPATH is inherited as-is: the engine is NOT prepended.
    assert env["PYTHONPATH"] == str(prior)


# --- cmd_run_parallel: split checkout and combined layout unchanged (11b) -------


def test_run_parallel_split_checkout_unchanged(tmp_path, monkeypatch):
    vault, engine, env = _run_parallel_env(tmp_path, monkeypatch, installed=False, prior=None)
    assert env["MACP_VAULT_ROOT"] == str(vault)
    assert env["PYTHONPATH"] == str(engine)


def test_run_parallel_split_checkout_prepends_engine_to_prior(tmp_path, monkeypatch):
    prior = tmp_path / "prior"
    vault, engine, env = _run_parallel_env(tmp_path, monkeypatch, installed=False, prior=prior)
    assert env["PYTHONPATH"].split(os.pathsep) == [str(engine), str(prior)]


def test_run_parallel_combined_layout_env_is_none(tmp_path, monkeypatch):
    vault = _vault_skeleton(tmp_path / "vault")
    monkeypatch.setattr(worker, "ROOT", vault)
    monkeypatch.setattr(worker, "WORKER_DIR", vault / "Worker")
    monkeypatch.setattr(worker, "_ENGINE_ROOT", vault.resolve())
    monkeypatch.setattr(worker, "queue_lock", contextlib.nullcontext)
    monkeypatch.setattr(worker, "load_queue", lambda *a, **k: {"runs": [{"status": "pending"}]})
    # Even a wheel: when the roots coincide nothing is handed over.
    monkeypatch.setattr(paths, "is_installed", lambda package_dir=None: True)
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda args, **kw: (calls.append((list(args), kw)), _FakeProc())[1])
    assert worker.cmd_run_parallel(1, dry_run=True) == 0
    spawned = [k for a, k in calls if "run-next" in a]
    assert len(spawned) == 1
    assert spawned[0].get("env") is None


def test_run_parallel_reads_is_installed_at_call_time():
    # The gate is a call, not an import-time constant: a monkeypatched
    # paths.is_installed (above) must be what cmd_run_parallel consults.
    src = (ENGINE / "todo_system" / "worker.py").read_text(encoding="utf-8")
    body = src[src.index("def cmd_run_parallel("):]
    body = body[:body.index("\ndef ", 1)]
    assert "if not paths.is_installed():" in body, body
    assert re.search(r"\"PYTHONPATH\"\s*:", body) is None, "PYTHONPATH must be conditional"


# --- beta_publish: scratch root + detached sweep cwd ----------------------------


def test_scratch_root_is_engine_root_in_checkout():
    root = paths.engine_root()
    assert not paths.is_installed()
    assert beta_publish._SCRATCH_ROOT == root
    assert beta_publish.LOCK_DIR == root / "Worker" / "_state" / "beta_locks"
    assert beta_publish.SWEEP_STATE_PATH == root / "Worker" / "_state" / "beta_sweep.json"
    assert beta_publish.SWEEP_WORKTREE_DIR == root / "Worker" / "_state" / "beta_worktrees"
    assert beta_publish.SWEEP_LOG_PATH == root / "Worker" / "_state" / "beta_sweep.log"


def test_scratch_root_source_is_layout_conditional():
    src = (ENGINE / "todo_system" / "orchestrator" / "beta_publish.py").read_text(encoding="utf-8")
    assert "_SCRATCH_ROOT = _VAULT_DATA_ROOT if paths.is_installed() else _ENGINE_ROOT" in src
    for name in ("LOCK_DIR", "SWEEP_STATE_PATH", "SWEEP_WORKTREE_DIR", "SWEEP_LOG_PATH"):
        line = re.search(rf"^{name}\s*=\s*(.*)$", src, re.M)
        assert line, name
        assert line.group(1).startswith("_SCRATCH_ROOT / "), (name, line.group(0))
    # The sweep is detached in the vault-data root, never the engine root.
    body = src[src.index("def _spawn_detached("):]
    body = body[:body.index("\ndef ", 1)]
    assert "cwd=str(_VAULT_DATA_ROOT)" in body
    assert "cwd=str(_ENGINE_ROOT)" not in body


def test_spawn_detached_cwd_is_vault_data_root(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(args, **kwargs):
        calls.append((list(args), kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.delenv("INVOCATION_ID", raising=False)   # plain spawn, no systemd scope
    log = tmp_path / "state" / "sweep.log"
    pid = beta_publish._spawn_detached(log_path=log)
    assert pid == 4242
    assert log.parent.is_dir()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [sys.executable, "-u", "-m", "todo_system.orchestrator.beta_publish", "sweep"]
    assert kwargs["cwd"] == str(beta_publish._VAULT_DATA_ROOT)
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] is subprocess.STDOUT
    # Combined layout: the vault-data root IS the engine root, so the cwd is
    # byte-identical to the former cwd=_ENGINE_ROOT. A standalone engine tree
    # (MACP_VAULT_ROOT elsewhere) only has to honour the vault root.
    if beta_publish._VAULT_DATA_ROOT == beta_publish._ENGINE_ROOT:
        assert Path(kwargs["cwd"]) == beta_publish._ENGINE_ROOT == paths.engine_root()


@pytest.mark.parametrize("name", ["LOCK_DIR", "SWEEP_STATE_PATH", "SWEEP_WORKTREE_DIR",
                                  "SWEEP_LOG_PATH"])
def test_scratch_paths_are_not_under_the_vault_data_tree_names(name):
    # Sanity on the derived names themselves (gitignored Worker/_state in both
    # layouts), so a wheel never writes scratch next to canon.
    path = getattr(beta_publish, name)
    assert path.parts[-3:-1] == ("Worker", "_state"), path
