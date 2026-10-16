"""The CLI's fallback calendar carries no personal calendar id (공개 위생 2026-10-16).

DEFAULT_CALENDAR_ID used to be a hard-coded group-calendar id; it now resolves through
paths.primary_calendar_id() and falls back to the account's own "primary" calendar.
"""

from __future__ import annotations

import importlib

from todo_system import cli, paths


def test_default_calendar_is_configured_or_primary(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_CALENDAR_ID", raising=False)
    monkeypatch.chdir(tmp_path)  # no .macp/config.toml above → neutral fallback
    assert paths.primary_calendar_id() == ""
    reloaded = importlib.reload(cli)
    try:
        assert reloaded.DEFAULT_CALENDAR_ID == "primary"
        assert "group.calendar.google.com" not in reloaded.DEFAULT_CALENDAR_ID
    finally:
        importlib.reload(cli)


def test_default_calendar_follows_env(monkeypatch):
    monkeypatch.setenv("MACP_CALENDAR_ID", "cal@example.com")
    reloaded = importlib.reload(cli)
    try:
        assert reloaded.DEFAULT_CALENDAR_ID == "cal@example.com"
    finally:
        monkeypatch.delenv("MACP_CALENDAR_ID", raising=False)
        importlib.reload(cli)
