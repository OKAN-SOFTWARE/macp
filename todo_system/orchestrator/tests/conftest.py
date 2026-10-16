"""Keep the orchestrator suite away from the live model cache and the real CLI.

`model_select.resolve_model` probes with a real `claude -p` call and writes
`Worker/_state/model_pref.json`. Any test that reaches `stages.stage_model()`
unpatched — build_stage_argv on a tier that is not cached, for instance — would
otherwise spend a real call and park a mocked/incidental choice in the LIVE
cache with a fresh 24h TTL (the trap test_model_select.ResolveTest documents).
Tests that want to exercise the probe or the cache patch these again on top.
"""

from __future__ import annotations

import subprocess

import pytest

from todo_system.orchestrator import model_select as _ms


class _NoProbeSubprocess:
    """Stands in for the `subprocess` module inside model_select, and nowhere else.

    The first version of this fixture did `setattr(_ms.subprocess, "run", fake)`.
    But `_ms.subprocess` IS the one global subprocess module object, shared by
    every module in the interpreter, so the fake landed on all of them. In the
    run of 2026-09-10 16:26 `jobs.start()` therefore launched nothing, recorded
    its `pid = -1` fallback, and the teardown's `jobs.stop()` called
    `os.kill(-1, SIGTERM)` — every process owned by the user died, including the
    Claude Code server and the desktop session.

    Rebinding the NAME in model_select's namespace keeps the fake where it
    belongs. Everything other than `run` proxies to the real module, and tests
    that want to drive the probe still patch `_ms.subprocess.run` on top of this,
    which now sets an attribute on this object instead of on the stdlib.
    """

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, *a, **k):
        return subprocess.CompletedProcess(a[0] if a else [], 0, "ok", "")


@pytest.fixture(autouse=True)
def _no_live_model_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(_ms, "_CACHE", tmp_path / "model_pref.json")
    monkeypatch.setattr(_ms, "subprocess", _NoProbeSubprocess())
    monkeypatch.delenv("WORKER_MODEL", raising=False)
