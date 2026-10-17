"""The suite must never touch this checkout's real runtime files.

Pins the two layers in todo_system/conftest.py: the per-test redirect of every
``_RUNTIME_PATHS`` row, and the import-time tripwire behind it. Both are reached
through fixtures (``real_runtime_paths``, ``runtime_tripwire``) only — importing
the conftest module from a test risks loading a second copy of it, and with it a
second audit hook.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import todo_system
from todo_system import cli, worker
from todo_system.orchestrator import message as m
from todo_system.orchestrator import metrics
from todo_system.orchestrator.mailbox import DEFAULT_ROOT, FileMailbox
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import ExecOutcome, Runner

REPO_ROOT = Path(todo_system.__file__).resolve().parents[1]
# The live notes the sync_* writers regenerate: (cli attribute, file under Todo/).
_NOTES = (("NOW_PATH", "NOW.md"), ("FOCUS_PATH", "FOCUS.md"),
          ("DASHBOARD_PATH", "Dashboard.md"))


def test_every_runtime_path_is_redirected(real_runtime_paths, real_runtime_anchors,
                                          runtime_tripwire, tmp_path_factory):
    # The files the suite used to leave behind, and the live notes, are covered
    # by name: a dropped row would otherwise just shrink the loop below.
    assert {(worker, "RUNNER_HEARTBEAT_PATH"), (worker, "QUEUE_LOCK_PATH"),
            (cli, "DB_PATH"), *((cli, attr) for attr, _ in _NOTES)} <= set(real_runtime_paths)
    # Independent of _RUNTIME_PATHS: cli.TODO_DIR is not redirected, so it names
    # the live notes, and the tripwire must refuse writes to them.
    assert {os.path.realpath(cli.TODO_DIR / name) for _, name in _NOTES} <= runtime_tripwire.protected
    base = tmp_path_factory.getbasetemp()
    for (module, attr), real in real_runtime_paths.items():
        now = getattr(module, attr)
        assert now != real, f"{module.__name__}.{attr} still points at the checkout"
        assert now.is_relative_to(base), f"{module.__name__}.{attr} = {now}"
        assert real.is_relative_to(real_runtime_anchors[module]), f"{module.__name__}.{attr} = {real}"
    # One real file, one redirect: metrics must read the DB cli writes.
    assert metrics.DB == cli.DB_PATH
    assert runtime_tripwire.protected == {os.path.realpath(p) for p in real_runtime_paths.values()}


def test_writers_land_in_the_redirect(runtime_tripwire, real_runtime_paths,
                                      tmp_path_factory):
    # Before any write: were a note row missing, the writers below would go to
    # the live note, so stop here instead.
    base = tmp_path_factory.getbasetemp()
    for attr, _ in _NOTES:
        assert getattr(cli, attr).is_relative_to(base), f"cli.{attr} = {getattr(cli, attr)}"
    before = len(runtime_tripwire.hits)
    worker.runner_heartbeat_start()
    with worker.queue_lock():
        pass
    con = cli.db_connect(cli.DB_PATH)
    try:
        cli.init_db(con)
        con.execute("INSERT INTO tasks (slug, title, path, status, imported_at) "
                    "VALUES ('t', 't', 't.md', 'done', 'now')")
        con.commit()
        cli.sync_now_note(con)
        cli.sync_focus_note(con)
        cli.sync_dashboard_note(con)
    finally:
        con.close()
    # Positive evidence: the writes happened — the heartbeat writer swallows
    # OSError, so a refused write would otherwise look exactly like this passing.
    for module, attr in real_runtime_paths:
        assert getattr(module, attr).exists(), f"{module.__name__}.{attr}"
    assert cli.NOW_PATH.read_text(encoding="utf-8")
    assert "# 📊 Dashboard" in cli.DASHBOARD_PATH.read_text(encoding="utf-8")
    assert metrics._db_counts()["done"] == 1
    assert len(runtime_tripwire.hits) == before


def test_poll_once_heartbeat_stays_in_the_redirect(tmp_path, runtime_tripwire):
    """poll_once takes the heartbeat for the task and deletes it afterwards — the
    pair that, unredirected, overwrote and removed the live runner's file."""
    seen = []

    class _Executor:
        mode = "fake"

        def execute(self, msg, agent, progress, **_):
            seen.append(worker.RUNNER_HEARTBEAT_PATH.exists())
            return ExecOutcome(True, "fake ok", {})

    mb = FileMailbox(tmp_path / "agents")
    agent = Agent(id="probe", kind="ai-local")
    mb.send(m.Envelope(to=agent.id, type=m.TASK_ASSIGN, task_id="t",
                       payload={"task_path": "Todo/items/t.md"}))
    before = len(runtime_tripwire.hits)

    assert Runner(mb, agent, _Executor(), log=lambda *_: None).poll_once() == 1

    assert seen == [True], "the task must run under a live heartbeat"
    assert not worker.RUNNER_HEARTBEAT_PATH.exists(), "and poll_once must release it"
    assert len(runtime_tripwire.hits) == before


def test_tripwire_blocks_and_records(tmp_path, monkeypatch, runtime_tripwire, request):
    state = tmp_path / "state"
    state.mkdir()
    p = state / "runner_heartbeat"          # absent: creating it must be refused
    q = state / "queue.lock"                # present: it must survive byte-identical
    q.write_text("real lock\n", encoding="utf-8")
    other = tmp_path / "other"
    other.write_text("replacement\n", encoding="utf-8")
    q_before = (q.read_bytes(), q.stat().st_mtime_ns)
    session_before = len(runtime_tripwire.hits)
    refused = runtime_tripwire.error

    with runtime_tripwire.guarding(p) as p_hits, runtime_tripwire.guarding(q) as q_hits:
        with pytest.raises(refused):
            open(p, "w")
        with pytest.raises(refused):
            sqlite3.connect(p)
        monkeypatch.chdir(state)
        with pytest.raises(refused):
            open("runner_heartbeat", "w")               # relative to a cwd inside the tree
        with pytest.raises(refused):
            os.remove(q)
        with pytest.raises(refused):
            os.utime(q)
        with pytest.raises(refused):
            os.replace(other, q)
        with pytest.raises(refused):
            shutil.rmtree(state)                        # an ancestor would take q with it
        # Reads stay allowed — the dashboard opens the real todo.db exactly so.
        assert q.read_text(encoding="utf-8") == "real lock\n"
        with pytest.raises(sqlite3.OperationalError):   # absent file, not the tripwire
            sqlite3.connect(f"file:{p}?mode=ro", uri=True)

    assert not p.exists()
    assert (q.read_bytes(), q.stat().st_mtime_ns) == q_before
    assert other.exists()
    hits = p_hits + q_hits
    assert sorted(h.event for h in hits) == sorted(
        ["open", "sqlite3.connect", "open", "os.remove", "os.utime", "os.rename",
         "shutil.rmtree"])
    assert {h.path for h in hits} == {os.path.realpath(p), os.path.realpath(q)}
    assert all(h.test.startswith(request.node.nodeid) for h in hits)
    assert all(h.stack for h in hits)
    # Kept off the session list: guarding() is for a test's own targets.
    assert len(runtime_tripwire.hits) == session_before


def test_suite_runs_with_vault_root_outside_the_checkout(tmp_path):
    """MACP_VAULT_ROOT outside the checkout — a worker stage testing a worktree
    while the env names the live vault — must not break the redirect.

    The rows are built from the env's vault root, not from this checkout, so a
    redirect keyed on the checkout failed every test at setup with ``ValueError:
    … is not in the subpath of …``. A child pytest gets the env, since the rows
    are captured when the conftest is imported.
    """
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text("", encoding="utf-8")
    before = sorted(vault.rglob("*"))
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST")}
    env["MACP_VAULT_ROOT"] = str(vault)
    here = Path(__file__).resolve()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'bt'}",
         f"{here}::test_every_runtime_path_is_redirected",
         f"{here}::test_writers_land_in_the_redirect"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "2 passed" in proc.stdout
    # The redirect did not leak into the vault the env names.
    assert sorted(vault.rglob("*")) == before


def test_redirect_target_for_a_vault_outside_the_checkout(
        real_runtime_paths, real_runtime_anchors, runtime_redirect_target,
        tmp_path_factory, tmp_path):
    # Rows re-derived from a vault that is not this checkout, as MACP_VAULT_ROOT
    # makes them. The old anchor cannot place them; the vault root can.
    fake_vault = tmp_path / "live-vault"
    redirect = tmp_path_factory.mktemp("probe")
    base = tmp_path_factory.getbasetemp()
    targets = {}
    for (module, attr), real in real_runtime_paths.items():
        fake_real = fake_vault / real.relative_to(real_runtime_anchors[module])
        with pytest.raises(ValueError):
            fake_real.relative_to(REPO_ROOT)
        target = runtime_redirect_target(redirect, fake_real, fake_vault)
        assert target.is_relative_to(redirect), f"{module.__name__}.{attr} = {target}"
        assert target.is_relative_to(base)
        assert not target.is_relative_to(fake_vault)
        targets[module, attr] = target
    assert targets[cli, "DB_PATH"] == targets[metrics, "DB"]


def test_both_live_mailbox_trees_are_guarded(runtime_tripwire):
    # The checkout's tree (the cwd-relative default) and the vault's; one tree
    # when MACP_VAULT_ROOT is unset or names this checkout.
    assert {os.path.realpath(REPO_ROOT / DEFAULT_ROOT),
            os.path.realpath(worker.ROOT / DEFAULT_ROOT)} <= runtime_tripwire.trees
