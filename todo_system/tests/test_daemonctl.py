"""Tests for todo_system.daemonctl label <-> unit mapping.

The real-account trading loop (`kis-trading-real`) is generated install-only by
scripts/systemd/gen_units.py; its launchd/systemd identity still needs a stable
label so daemonctl (restart/status hints, log stems) can name it like the others.

Hermetic by construction: every case injects the org prefix (``com.example.``)
instead of inheriting whatever the cwd vault's ``.macp/config.toml`` pins, so the
assertions carry no installed host's identity. The installed vault's live labels
are guarded where they are pinned — the ``label_prefix`` key of its config — and by
test_paths' resolver cases.
"""

from __future__ import annotations

import importlib
import os
import subprocess

import pytest

from todo_system import daemonctl

PREFIX = "com.example."


@pytest.fixture
def example_units(monkeypatch):
    """daemonctl's module maps rebuilt from the injected prefix. unit_name /
    label_for read the module globals at call time, so patching them is enough."""
    units = daemonctl.build_units(PREFIX)
    monkeypatch.setattr(daemonctl, "UNITS", units)
    monkeypatch.setattr(daemonctl, "LABELS", {u: lbl for lbl, u in units.items()})
    return units


class FakeRun:
    """Stand-in for ``daemonctl._run`` — the module's one subprocess boundary.
    Records every ``cmd`` it is called with and lets a test either queue
    responses (consumed in call order) or pin a response to calls matching a
    predicate (checked before the queue, in registration order). Never shells
    out to ``launchctl``/``systemctl`` — callers get canned
    ``CompletedProcess``/exception results only."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._queue: list[tuple[str, object]] = []
        self._by_match: list[tuple[object, str | None, BaseException | None]] = []

    def queue_stdout(self, stdout: str) -> None:
        self._queue.append(("stdout", stdout))

    def queue_exception(self, exc: BaseException) -> None:
        self._queue.append(("exc", exc))

    def on_cmd(self, predicate, *, stdout: str | None = None, exc: BaseException | None = None) -> None:
        self._by_match.append((predicate, stdout, exc))

    def __call__(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        for predicate, stdout, exc in self._by_match:
            if predicate(cmd):
                if exc is not None:
                    raise exc
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout or "", stderr="")
        if self._queue:
            kind, val = self._queue.pop(0)
            if kind == "exc":
                raise val
            return subprocess.CompletedProcess(cmd, 0, stdout=val, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def fake_run(monkeypatch):
    fr = FakeRun()
    monkeypatch.setattr(daemonctl, "_run", fr)
    return fr


@pytest.fixture
def set_platform(monkeypatch):
    def _set(darwin: bool) -> None:
        monkeypatch.setattr(daemonctl, "IS_DARWIN", darwin)

    return _set


def test_kis_trading_real_label_maps_to_unit(example_units):
    assert daemonctl.UNITS["com.example.kis-trading-real"] == "kis-trading-real"


def test_unit_name_resolves_real_label(example_units):
    assert daemonctl.unit_name("com.example.kis-trading-real") == "kis-trading-real"


def test_label_for_resolves_real_unit(example_units):
    assert daemonctl.label_for("kis-trading-real") == "com.example.kis-trading-real"


def test_real_label_is_outside_the_secondbrain_health_sweep():
    # health_report.py and session_end.py sweep loaded_pids() with a
    # `<prefix>secondbrain.` / `secondbrain` prefix. An install-only unit that a
    # human has deliberately NOT started must not read back as a down daemon, so
    # its label must fall outside that prefix (same as the paper kis-trading*).
    assert not "com.example.kis-trading-real".startswith("com.example.secondbrain.")


# The exact label -> unit dict the daemons shipped with before build_units()
# assembled it from a prefix, re-keyed onto the injected prefix. Kept as an
# independent literal (NOT derived from build_units) so it catches any drift in
# the assembly logic: label suffixes, systemd unit stems and the log-file names
# built from them must not move.
_LEGACY_UNITS = {
    "com.example.secondbrain.runner.worker-default": "macp-runner-worker-default",
    "com.example.secondbrain.orchestrator": "macp-orchestrator",
    "com.example.secondbrain.planner": "macp-planner",
    "com.example.secondbrain.maintenance": "macp-maintenance",
    "com.example.secondbrain.macp-monitor": "macp-monitor",
    "com.example.secondbrain.todo-sync": "macp-todo-sync",
    "com.example.secondbrain.dashboard": "macp-dashboard",
    "com.example.secondbrain.brief": "macp-brief",
    "com.example.secondbrain.kakao-refresh": "macp-kakao-refresh",
    "com.example.secondbrain.health": "macp-health",
    "com.example.claude-auth-watch": "claude-auth-watch",
    "com.example.claude-upgrade": "claude-upgrade",
    "com.example.claude-remote-control": "claude-remote-control",
    "com.example.claude-remote-control-watchdog": "claude-remote-control-watchdog",
    "com.example.mailroom.tick": "mailroom-tick",
    "com.example.kis-trading": "kis-trading",
    "com.example.kis-trading-livesync": "kis-trading-livesync",
    "com.example.kis-trading-watchdog": "kis-trading-watchdog",
    "com.example.kis-trading-real": "kis-trading-real",
}


def test_build_units_prefix_is_byte_identical_to_legacy():
    # Live-daemon safety: for a given org prefix the assembled dict must equal the
    # pre-resolver literal — same keys, same values, same insertion order.
    built = daemonctl.build_units(PREFIX)
    assert built == _LEGACY_UNITS
    assert list(built.items()) == list(_LEGACY_UNITS.items())


def test_module_units_follow_label_prefix_at_import(monkeypatch):
    # UNITS is built from label_prefix() at import: with MACP_LABEL_PREFIX injected
    # the module-level map must equal the legacy literal for that prefix.
    with monkeypatch.context() as m:
        m.setenv("MACP_LABEL_PREFIX", PREFIX)
        mod = importlib.reload(daemonctl)
        assert mod.LABEL_PREFIX == PREFIX
        assert mod.SECONDBRAIN_PREFIX == "com.example.secondbrain."
        assert mod.UNITS == _LEGACY_UNITS
    importlib.reload(daemonctl)               # restore the cwd-resolved maps for later tests


def test_build_units_reprefixes_every_label():
    u = daemonctl.build_units("acme.")
    assert all(label.startswith("acme.") for label in u)
    # Both product-namespaced and org-level labels re-prefix; unit stems are unchanged.
    assert u["acme.secondbrain.orchestrator"] == "macp-orchestrator"
    assert u["acme.kis-trading-real"] == "kis-trading-real"
    assert list(u.values()) == list(_LEGACY_UNITS.values())


def test_build_units_preserves_two_tier_namespace():
    # secondbrain. stays a product sub-namespace INSIDE the org prefix, so an
    # org-level install-only unit (kis-trading-real) stays OUTSIDE the health
    # sweep's f"{prefix}secondbrain." filter even under a custom prefix.
    u = daemonctl.build_units("acme.")
    assert "acme.kis-trading-real" in u
    assert "acme.secondbrain.kis-trading-real" not in u
    assert not "acme.kis-trading-real".startswith("acme.secondbrain.")


# ---------------------------------------------------------------------------
# restart / restart_hint / status_hint — darwin/linux branches, all via
# fake_run/set_platform so no real launchctl/systemctl is ever invoked.
# ---------------------------------------------------------------------------


def test_restart_darwin_invokes_launchctl_kickstart(example_units, fake_run, set_platform):
    set_platform(True)
    label = "com.example.secondbrain.todo-sync"
    fake_run.queue_stdout("")
    daemonctl.restart(label)
    assert fake_run.calls == [["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]]


def test_restart_linux_invokes_systemctl_with_mapped_unit(example_units, fake_run, set_platform):
    set_platform(False)
    label = "com.example.secondbrain.todo-sync"
    fake_run.queue_stdout("")
    daemonctl.restart(label)
    assert fake_run.calls == [["systemctl", "--user", "restart", "macp-todo-sync.service"]]


def test_restart_hint_darwin(set_platform, fake_run):
    set_platform(True)
    label = "com.example.todo-sync"
    assert daemonctl.restart_hint(label) == f"launchctl kickstart -k gui/$(id -u)/{label}"
    assert fake_run.calls == []


def test_restart_hint_linux(example_units, set_platform, fake_run):
    set_platform(False)
    label = "com.example.secondbrain.todo-sync"
    assert daemonctl.restart_hint(label) == "systemctl --user restart macp-todo-sync.service"
    assert fake_run.calls == []


def test_status_hint_darwin(set_platform, fake_run):
    set_platform(True)
    label = "com.example.todo-sync"
    assert daemonctl.status_hint(label) == f"launchctl print gui/$(id -u)/{label}"
    assert fake_run.calls == []


def test_status_hint_linux(example_units, set_platform, fake_run):
    set_platform(False)
    label = "com.example.secondbrain.todo-sync"
    assert daemonctl.status_hint(label) == "systemctl --user status macp-todo-sync.service"
    assert fake_run.calls == []


# ---------------------------------------------------------------------------
# main_pid — linux (4 cases + unknown-label identity) and darwin (2 cases)
# ---------------------------------------------------------------------------


def test_main_pid_linux_loaded_running(example_units, fake_run, set_platform):
    set_platform(False)
    label = "com.example.secondbrain.todo-sync"
    fake_run.queue_stdout("LoadState=loaded\nMainPID=1234\n")
    assert daemonctl.main_pid(label) == 1234
    assert fake_run.calls == [
        ["systemctl", "--user", "show", "-p", "LoadState", "-p", "MainPID", "macp-todo-sync.service"]
    ]


def test_main_pid_linux_loaded_idle(example_units, fake_run, set_platform):
    set_platform(False)
    fake_run.queue_stdout("LoadState=loaded\nMainPID=0\n")
    assert daemonctl.main_pid("com.example.secondbrain.todo-sync") is None


def test_main_pid_linux_not_loaded(example_units, fake_run, set_platform):
    set_platform(False)
    fake_run.queue_stdout("LoadState=not-found\nMainPID=1234\n")
    assert daemonctl.main_pid("com.example.secondbrain.todo-sync") is None


@pytest.mark.parametrize("exc", [OSError("boom"), subprocess.SubprocessError("boom")])
def test_main_pid_linux_run_raises(example_units, fake_run, set_platform, exc):
    set_platform(False)
    fake_run.queue_exception(exc)
    assert daemonctl.main_pid("com.example.secondbrain.todo-sync") is None


def test_main_pid_linux_non_integer_pid(example_units, fake_run, set_platform):
    set_platform(False)
    fake_run.queue_stdout("LoadState=loaded\nMainPID=abc\n")
    assert daemonctl.main_pid("com.example.secondbrain.todo-sync") is None


def test_main_pid_linux_unknown_label_uses_identity_unit(fake_run, set_platform):
    # unit_name() falls back to identity for labels outside UNITS — a regression
    # guard so an unmapped label still queries *something* sane.
    set_platform(False)
    fake_run.queue_stdout("LoadState=loaded\nMainPID=42\n")
    assert daemonctl.main_pid("unmapped-label") == 42
    assert fake_run.calls == [
        ["systemctl", "--user", "show", "-p", "LoadState", "-p", "MainPID", "unmapped-label.service"]
    ]


def test_main_pid_darwin_delegates_to_loaded_pids(monkeypatch, set_platform, fake_run):
    set_platform(True)
    captured = {}

    def stub_loaded_pids(prefix=None):
        captured["prefix"] = prefix
        return {"com.example.todo-sync": 555}

    monkeypatch.setattr(daemonctl, "loaded_pids", stub_loaded_pids)
    assert daemonctl.main_pid("com.example.todo-sync") == 555
    assert captured["prefix"] == ""
    assert fake_run.calls == []


def test_main_pid_darwin_none_when_loaded_pids_none(monkeypatch, set_platform):
    set_platform(True)
    monkeypatch.setattr(daemonctl, "loaded_pids", lambda prefix=None: None)
    assert daemonctl.main_pid("com.example.todo-sync") is None


# ---------------------------------------------------------------------------
# loaded_pids — darwin parsing branches + linux failure-mode split
# ---------------------------------------------------------------------------


def test_loaded_pids_darwin_tab_separated(set_platform, fake_run):
    set_platform(True)
    fake_run.queue_stdout("1234\t0\tcom.example.secondbrain.todo-sync\n")
    pids = daemonctl.loaded_pids(prefix="com.example.secondbrain.")
    assert pids == {"com.example.secondbrain.todo-sync": 1234}


def test_loaded_pids_darwin_space_separated(set_platform, fake_run):
    set_platform(True)
    fake_run.queue_stdout("1234 0 com.example.secondbrain.todo-sync\n")
    pids = daemonctl.loaded_pids(prefix="com.example.secondbrain.")
    assert pids == {"com.example.secondbrain.todo-sync": 1234}


def test_loaded_pids_darwin_skips_short_lines(set_platform, fake_run):
    set_platform(True)
    fake_run.queue_stdout("1234\tcom.example.secondbrain.todo-sync\n")
    pids = daemonctl.loaded_pids(prefix="com.example.secondbrain.")
    assert pids == {}


def test_loaded_pids_darwin_non_integer_pid_is_none(set_platform, fake_run):
    set_platform(True)
    fake_run.queue_stdout("-\t0\tcom.example.secondbrain.todo-sync\n")
    pids = daemonctl.loaded_pids(prefix="com.example.secondbrain.")
    assert pids == {"com.example.secondbrain.todo-sync": None}


def test_loaded_pids_darwin_excludes_labels_outside_prefix(set_platform, fake_run):
    set_platform(True)
    fake_run.queue_stdout(
        "1234\t0\tcom.example.secondbrain.todo-sync\n"
        "5678\t0\tcom.example.kis-trading-real\n"
    )
    pids = daemonctl.loaded_pids(prefix="com.example.secondbrain.")
    assert pids == {"com.example.secondbrain.todo-sync": 1234}
    assert "com.example.kis-trading-real" not in pids


def test_loaded_pids_linux_file_not_found_returns_none(example_units, fake_run, set_platform):
    set_platform(False)
    fake_run.queue_exception(FileNotFoundError("no systemctl"))
    assert daemonctl.loaded_pids(prefix="com.example.secondbrain.") is None


def test_loaded_pids_linux_skips_unit_with_subprocess_error(example_units, fake_run, set_platform):
    set_platform(False)

    def is_todo_sync(cmd):
        return "macp-todo-sync.service" in cmd

    fake_run.on_cmd(is_todo_sync, exc=subprocess.SubprocessError("boom"))
    fake_run.on_cmd(lambda cmd: True, stdout="LoadState=loaded\nMainPID=99\n")
    pids = daemonctl.loaded_pids(prefix="com.example.secondbrain.")
    assert "com.example.secondbrain.todo-sync" not in pids
    assert pids  # the other secondbrain units in example_units still came back
    assert all(pid == 99 for pid in pids.values())


def test_loaded_pids_default_prefix_resolved_at_call_time(monkeypatch, set_platform, fake_run):
    # prefix=None must resolve label_prefix() at *call* time, not import time, so
    # a MACP_LABEL_PREFIX set after import is honoured.
    set_platform(True)
    monkeypatch.setenv("MACP_LABEL_PREFIX", "acme.")
    fake_run.queue_stdout(
        "1234\t0\tacme.secondbrain.todo-sync\n"
        "5678\t0\tacme.other-thing\n"
    )
    pids = daemonctl.loaded_pids()
    assert pids == {"acme.secondbrain.todo-sync": 1234}
