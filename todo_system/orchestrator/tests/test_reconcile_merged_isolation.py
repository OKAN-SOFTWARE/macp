"""reconcile_merged isolates each ledger row, like the other reconcile sweeps.

The sweep used to wrap its whole row loop in one `try` ending in a bare
`except Exception: pass`, so a single row raising (gh, todo.db, ledger write)
silently dropped every merged-PR row sorted after it from that tick. These
tests pin the per-row net: the bad row is skipped and named, the rows behind
it still close, and a broken sweep scaffold is logged instead of swallowed.

Ledger.all() returns rows in filename order, so `a-bad` is always visited
before `b-good`.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import stages, tasks
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator
from todo_system.orchestrator.registry import Agent, Registry


class ReconcileMergedRowIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = Ledger(self.tmp / "assignments")
        self.sent: list = []
        self.orch = Orchestrator(
            FileMailbox(self.tmp / "agents"),
            Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")]),
            self.ledger,
            notify=lambda t, level=None: self.sent.append(t))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, tid: str) -> None:
        # last_alert_kind left empty on purpose: _record_false_alarm then never
        # writes an episode (it would land outside the tmp vault).
        self.ledger.save(Assignment(task_id=tid, agent_id="w", corr_id=f"c-{tid}",
                                    kind="ai-local", state=A.AWAITING_HUMAN))

    def _sweep(self, merged, **extra):
        """Run reconcile_merged with gh/todo/close all mocked; capture stdout."""
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(stages, "gh_pr_merged",
                                                  side_effect=merged))
            stack.enter_context(mock.patch.object(stages, "gh_pr_state",
                                                  return_value=None))
            if "task_status" not in extra:
                stack.enter_context(mock.patch.object(tasks, "task_status",
                                                      return_value="open"))
            for patcher in extra.values():
                stack.enter_context(patcher)
            close = stack.enter_context(
                mock.patch.object(self.orch, "_close_with_evidence"))
            stack.enter_context(contextlib.redirect_stdout(buf))
            out = self.orch.reconcile_merged()
        return out, buf.getvalue(), close

    def test_gh_error_on_one_row_still_closes_the_merged_row_behind_it(self):
        self._row("a-bad")
        self._row("b-good")

        def merged(branch, repo=None):
            if branch == "worker/a-bad":
                raise RuntimeError("gh boom")
            return True

        out, printed, close = self._sweep(merged)
        self.assertEqual([a.task_id for a in out], ["b-good"])
        self.assertEqual(self.ledger.load("b-good").state, A.DONE)
        self.assertEqual(self.ledger.load("a-bad").state, A.AWAITING_HUMAN)
        self.assertIn("[orchestrator] reconcile_merged: a-bad skipped: gh boom", printed)
        close.assert_called_once()
        self.assertEqual(close.call_args.args[0].task_id, "b-good")

    def test_todo_db_error_on_one_row_still_closes_the_merged_row_behind_it(self):
        self._row("a-bad")
        self._row("b-good")

        def merged(branch, repo=None):
            return None if branch == "worker/a-bad" else True

        locked = mock.patch.object(
            tasks, "task_status",
            side_effect=sqlite3.OperationalError("database is locked"))
        out, printed, _ = self._sweep(merged, task_status=locked)
        self.assertEqual([a.task_id for a in out], ["b-good"])
        self.assertEqual(self.ledger.load("b-good").state, A.DONE)
        self.assertIn("reconcile_merged: a-bad skipped: database is locked", printed)

    def test_ledger_write_error_on_one_row_still_closes_the_merged_row_behind_it(self):
        self._row("a-bad")
        self._row("b-good")
        orig = self.ledger.save

        def save(a):
            if a.task_id == "a-bad":
                raise OSError("disk full")
            return orig(a)

        failing = mock.patch.object(self.ledger, "save", side_effect=save)
        out, printed, _ = self._sweep(lambda branch, repo=None: True, save=failing)
        self.assertEqual([a.task_id for a in out], ["b-good"])
        self.assertEqual(self.ledger.load("b-good").state, A.DONE)
        self.assertIn("reconcile_merged: a-bad skipped: disk full", printed)

    def test_broken_sweep_scaffold_is_logged_not_swallowed(self):
        broken = mock.patch.object(self.ledger, "all",
                                   side_effect=OSError("ledger gone"))
        out, printed, _ = self._sweep(lambda branch, repo=None: True, all=broken)
        self.assertEqual(out, [])                     # never raises into tick()
        self.assertIn("reconcile_merged: sweep aborted: ledger gone", printed)

    def test_clean_merged_rows_all_close_with_no_skip_lines(self):
        self._row("a-bad")
        self._row("b-good")
        out, printed, close = self._sweep(lambda branch, repo=None: True)
        self.assertEqual(sorted(a.task_id for a in out), ["a-bad", "b-good"])
        self.assertEqual(self.ledger.load("a-bad").state, A.DONE)
        self.assertEqual(self.ledger.load("b-good").state, A.DONE)
        self.assertEqual(close.call_count, 2)
        self.assertNotIn("skipped:", printed)


if __name__ == "__main__":
    unittest.main()
