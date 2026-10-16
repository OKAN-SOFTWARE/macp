"""Orchestrator._queued_task_ids skips a mailbox it cannot read, but not a
refused write.

The reaper asks every agent's mailbox for queued TASK_ASSIGNs and skips one it
cannot reach, so a single bad mailbox does not block reaping the rest. A bare
``except Exception`` there also swallowed the conftest tripwire's refusal
(todo_system/conftest.py), so a test writing into the live mailbox through this
path passed quietly instead of failing.
"""

from __future__ import annotations

import pytest

from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator
from todo_system.orchestrator.registry import Agent, Registry


class _Stub:
    def __init__(self, receive):
        self._receive = receive

    def receive(self, agent_id):
        return self._receive(agent_id)


def _raises(exc):
    def receive(agent_id):
        raise exc
    return receive


def _queued_b(agent_id):
    return [m.Envelope(to=agent_id, type=m.TASK_ASSIGN, task_id="t-b")]


def _orch(tmp_path, monkeypatch, receive_a):
    reg = Registry([Agent(id="a", kind="ai-local"), Agent(id="b", kind="ai-local")])
    orch = Orchestrator(FileMailbox(tmp_path / "agents"), reg, Ledger(tmp_path / "assignments"),
                        notify=lambda *a, **k: None)
    stubs = {"a": _Stub(receive_a), "b": _Stub(_queued_b)}
    monkeypatch.setattr(orch, "_mailbox_for", lambda agent: stubs[agent.id])
    return orch


def test_unreachable_mailbox_is_skipped(tmp_path, monkeypatch):
    orch = _orch(tmp_path, monkeypatch, _raises(FileNotFoundError("gone")))
    assert orch._queued_task_ids() == {"t-b"}


@pytest.mark.parametrize("exc", [KeyError("to"), ValueError("unknown type"),
                                 TypeError("not a dict"), PermissionError("unreadable")])
def test_malformed_or_unreadable_mailbox_is_skipped(tmp_path, monkeypatch, exc):
    # Envelope.from_dict raises these out of receive(); one bad envelope must
    # not take the reaper down.
    orch = _orch(tmp_path, monkeypatch, _raises(exc))
    assert orch._queued_task_ids() == {"t-b"}


def test_tripwire_refusal_propagates(tmp_path, monkeypatch, runtime_tripwire):
    guarded = tmp_path / "cursor.json"

    def receive(agent_id):
        guarded.write_text("{}", encoding="utf-8")      # through the real audit hook
        return []

    orch = _orch(tmp_path, monkeypatch, receive)
    with runtime_tripwire.guarding(guarded) as hits:
        with pytest.raises(runtime_tripwire.error):
            orch._queued_task_ids()
    assert [h.event for h in hits] == ["open"]
    assert not guarded.exists()


def test_tripwire_refusal_propagates_out_of_reap_stale(tmp_path, monkeypatch, runtime_tripwire):
    guarded = tmp_path / "cursor.json"

    def receive(agent_id):
        guarded.write_text("{}", encoding="utf-8")
        return []

    orch = _orch(tmp_path, monkeypatch, receive)
    with runtime_tripwire.guarding(guarded) as hits:
        with pytest.raises(runtime_tripwire.error):
            orch.reap_stale()
    assert len(hits) == 1
