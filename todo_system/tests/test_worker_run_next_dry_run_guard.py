"""run-next --dry-run must not depend on the live usage guard.

A dry-run launches no Claude process, so a pacing hold on the host's account
must not turn it into rc=3 (the installed-wheel smoke and the engine-isolation
leg C both run it against a throwaway vault). The non-dry-run hold is pinned
alongside as the positive control.
"""

from __future__ import annotations

import json

import pytest

from todo_system import usage_guard, worker


@pytest.fixture
def hermetic(monkeypatch, tmp_path):
    calls: dict[str, list] = {"guard": [], "claim": [], "idle": [], "to_main": []}

    def fake_check(*args, **kwargs):
        calls["guard"].append((args, kwargs))
        return {"allowed": False, "reason": "injected hold"}

    def fake_claim():
        calls["claim"].append(True)
        return None

    def fake_idle(dry_run=False):
        calls["idle"].append(dry_run)
        return 0

    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps({"runs": []}), encoding="utf-8")

    # The live vault's own git status must not short-circuit into rc=2.
    monkeypatch.setattr(worker, "vault_is_dirty_outside_worker", lambda: (False, []))
    monkeypatch.setattr(worker, "clear_dirty_stall", lambda: None)
    monkeypatch.setattr(worker, "record_dirty_stall", lambda bad_files: None)
    monkeypatch.setattr(worker, "QUEUE_PATH", queue_path)
    monkeypatch.setattr(worker, "claim_next_pending", fake_claim)
    monkeypatch.setattr(worker, "_handle_idle_planner", fake_idle)
    monkeypatch.setattr(
        worker, "_return_vault_to_main",
        lambda own_branch=None: calls["to_main"].append(own_branch),
    )
    monkeypatch.setattr(usage_guard, "check", fake_check)
    return calls, queue_path


def test_dry_run_ignores_guard_hold_and_leaves_queue_untouched(hermetic, capsys):
    calls, queue_path = hermetic
    before = queue_path.read_bytes()

    rc = worker.run_next(dry_run=True)

    assert rc == 0
    assert queue_path.read_bytes() == before
    assert calls["guard"] == []
    assert calls["claim"] == []
    assert calls["idle"] == [True]
    assert calls["to_main"] == []
    assert "no pending runs" in capsys.readouterr().out


def test_non_dry_run_still_holds_with_rc3(hermetic, capsys):
    calls, queue_path = hermetic
    before = queue_path.read_bytes()

    rc = worker.run_next(dry_run=False)

    assert rc == 3
    assert len(calls["guard"]) == 1
    assert calls["claim"] == []
    assert calls["idle"] == []
    assert queue_path.read_bytes() == before
    assert "usage guard blocked: injected hold" in capsys.readouterr().out
