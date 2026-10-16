"""/api/status: a FAILED-but-waiting ledger row (checkout_busy / job_running)
is shown as `deferred`, carries a `deferred: true` flag for clients that do not
know the new state (the Android app), and is kept out of `attention`."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[2] / "scripts" / "macp_dashboard.py"


def _load():
    spec = importlib.util.spec_from_file_location("macp_dashboard_deferred", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


class DeferredStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        assign = self.tmp / "assign"
        assign.mkdir()
        rows = {
            "waiting": {"state": "failed", "checkout_busy": True,
                        "last_note": "held by a LIVE sibling run"},
            "job": {"state": "failed", "job_running": True},
            "broken": {"state": "failed", "last_note": "tests red"},
            "parked": {"state": "awaiting_human"},
        }
        for slug, d in rows.items():
            (assign / f"{slug}.json").write_text(json.dumps(
                {"task_id": slug, "agent_id": "w", "updated_ts": "2026-09-25T10:00:00",
                 **d}), encoding="utf-8")
        patches = [mock.patch.object(M, "ASSIGN", assign),
                   mock.patch.object(M, "RUNS", self.tmp / "runs"),
                   mock.patch.object(M, "STATE", self.tmp / "_state"),
                   mock.patch.object(M, "TODO_DB", self.tmp / "todo.db")]
        for name, val in (("collect_human", []), ("collect_deps", []),
                          ("collect_agents", []), ("collect_todo", {}),
                          ("collect_usage", None), ("_worker_running", []),
                          ("_todo_status_map", {}), ("_title_map", {}),
                          ("_project_map", {})):
            patches.append(mock.patch.object(M, name, return_value=val))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_rows_and_attention(self):
        d = M.collect_status()
        by = {r["task"]: r for r in d["recent"]}
        self.assertEqual(by["waiting"]["state"], "deferred")
        self.assertIs(by["waiting"]["deferred"], True)
        self.assertEqual(by["job"]["state"], "deferred")
        self.assertEqual(by["broken"]["state"], "failed")
        self.assertIs(by["broken"]["deferred"], False)
        attention = {r["task"] for r in d["attention"]}
        self.assertEqual(attention, {"broken", "parked"})

    def test_ledger_row_detail(self):
        r = M._ledger_row("waiting")
        self.assertEqual((r["state"], r["deferred"]), ("deferred", True))

    def test_web_badge_knows_deferred(self):
        self.assertIn("deferred:'b-def'", M.HTML)
        self.assertIn(".b-def{", M.HTML)


if __name__ == "__main__":
    unittest.main()
