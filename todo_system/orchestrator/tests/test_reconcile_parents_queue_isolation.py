"""reconcile_parents and reconcile_queue isolate each row, like the other sweeps.

Both used to wrap their whole row loop in one `try` ending in
`except Exception: return []` / `pass`, so a single parent row or queue entry
raising silently dropped every row behind it from that tick. These tests pin
the per-row net: the bad row is skipped and named, the rows behind it are still
processed, and a broken sweep scaffold is logged instead of swallowed.

Rows are visited in list order, so `a-bad` always comes before `b-good`.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import worker as _worker
from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import repos, tasks
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator
from todo_system.orchestrator.registry import Agent, Registry


def _parent(slug: str) -> dict:
    return {"slug": slug, "title": slug, "n_children": 2}


class ReconcileParentsRowIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = Ledger(self.tmp / "assignments")
        self.pings: list[str] = []
        self.orch = Orchestrator(
            FileMailbox(self.tmp / "agents"),
            Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")]),
            self.ledger,
            notify=lambda text, *a, **k: self.pings.append(text))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sweep(self, rows, frontmatter, mark_done=None, **extra):
        """Run reconcile_parents with the db query and md reads mocked; capture stdout."""
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            if "review" not in extra:
                stack.enter_context(mock.patch.object(
                    tasks, "parents_needing_review", return_value=rows))
            stack.enter_context(mock.patch.object(
                repos, "_frontmatter", side_effect=frontmatter))
            if mark_done is not None:
                stack.enter_context(mock.patch.object(
                    tasks, "mark_done", side_effect=mark_done))
            for patcher in extra.values():
                stack.enter_context(patcher)
            stack.enter_context(contextlib.redirect_stdout(buf))
            out = self.orch.reconcile_parents()
        return out, buf.getvalue()

    def test_frontmatter_error_on_one_parent_still_surfaces_the_parent_behind_it(self):
        rows = [_parent("a-bad"), _parent("b-good")]

        def fm(path):
            if Path(path).stem == "a-bad":
                raise OSError("fm boom")
            return {}

        out, printed = self._sweep(rows, fm)
        self.assertEqual(out, ["b-good"])
        self.assertEqual(len(self.pings), 1)
        self.assertIn("b-good", self.pings[0])
        self.assertNotIn("a-bad", self.pings[0])
        self.assertIn("[orchestrator] reconcile_parents: a-bad skipped: fm boom", printed)

        # The skipped row was never recorded as surfaced, so it retries next tick.
        out, printed = self._sweep(rows, lambda path: {})
        self.assertEqual(out, ["a-bad"])
        self.assertNotIn("skipped:", printed)

    def test_mark_done_error_on_one_bundle_still_closes_the_bundle_behind_it(self):
        rows = [_parent("a-bad"), _parent("b-good")]

        def mark_done(slug):
            if slug == "a-bad":
                raise RuntimeError("db locked")
            return True

        out, printed = self._sweep(rows, lambda path: {"auto_rescoped": "true"},
                                   mark_done=mark_done)
        self.assertEqual(out, ["b-good"])
        self.assertIn("reconcile_parents: a-bad skipped: db locked", printed)
        self.assertIn("auto-closed rescope bundle b-good", printed)
        self.assertEqual(self.pings, [])

    def test_broken_sweep_scaffold_is_logged_not_swallowed(self):
        broken = mock.patch.object(tasks, "parents_needing_review",
                                   side_effect=RuntimeError("boom"))
        out, printed = self._sweep([], lambda path: {}, review=broken)
        self.assertEqual(out, [])                     # never raises into tick()
        self.assertIn("reconcile_parents: sweep aborted: boom", printed)

    def test_clean_rows_have_no_skip_lines(self):
        rows = [_parent("a-bad"), _parent("b-good")]
        out, printed = self._sweep(rows, lambda path: {})
        self.assertEqual(out, ["a-bad", "b-good"])
        self.assertEqual(len(self.pings), 1)
        self.assertNotIn("skipped:", printed)


class ReconcileQueueRowIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = Ledger(self.tmp / "assignments")
        self.orch = Orchestrator(
            FileMailbox(self.tmp / "agents"),
            Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")]),
            self.ledger,
            notify=lambda *a, **k: None)
        self.queue: dict = {"runs": []}
        self.lock_held = False
        self.saves: list[tuple[bool, dict]] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _done(self, *slugs: str) -> None:
        for slug in slugs:
            self.ledger.save(Assignment(task_id=slug, agent_id="w", corr_id=f"c-{slug}",
                                        kind="ai-local", state=A.DONE))

    @contextlib.contextmanager
    def _lock(self):
        self.lock_held = True
        try:
            yield
        finally:
            self.lock_held = False

    def _save(self, q):
        self.saves.append((self.lock_held, q))

    def _sweep(self, **extra):
        """Run reconcile_queue with the queue file and lock faked; capture stdout."""
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            if "load" not in extra:
                stack.enter_context(mock.patch.object(
                    _worker, "load_queue", lambda: self.queue))
            stack.enter_context(mock.patch.object(_worker, "save_queue", self._save))
            stack.enter_context(mock.patch.object(_worker, "queue_lock", self._lock))
            for patcher in extra.values():
                stack.enter_context(patcher)
            stack.enter_context(contextlib.redirect_stdout(buf))
            out = self.orch.reconcile_queue()
        return out, buf.getvalue()

    def _unclaimable_raising_for(self, bad: str):
        def unclaimable(run, assigned):
            if run.get("slug") == bad:
                raise RuntimeError("md boom")
            return False
        return mock.patch.object(self.orch, "_unclaimable", side_effect=unclaimable)

    def test_error_on_one_entry_still_completes_the_entry_behind_it(self):
        self._done("b-good")
        self.queue = {"runs": [{"slug": "a-bad", "status": "pending"},
                               {"slug": "b-good", "status": "pending"}]}
        out, printed = self._sweep(unclaimable=self._unclaimable_raising_for("a-bad"))
        self.assertEqual(out, ["b-good"])
        bad, good = self.queue["runs"]
        self.assertEqual(good["status"], "completed")
        self.assertTrue(good["finished_at"])
        self.assertEqual(bad["status"], "pending")
        self.assertNotIn("finished_at", bad)          # never left half-written
        self.assertEqual(len(self.saves), 1)          # once, after the loop
        self.assertIs(self.saves[0][0], True)         # under the lock
        self.assertIn("[orchestrator] reconcile_queue: a-bad skipped: md boom", printed)

    def test_non_dict_entry_is_skipped_and_the_rest_reconciled(self):
        self._done("b-good")
        self.queue = {"runs": ["garbage", {"slug": "b-good", "status": "pending"}]}
        out, printed = self._sweep()
        self.assertEqual(out, ["b-good"])
        self.assertEqual(self.queue["runs"][1]["status"], "completed")
        self.assertIn("reconcile_queue: ? skipped:", printed)
        self.assertEqual(len(self.saves), 1)

    def test_all_entries_failing_writes_nothing(self):
        self.queue = {"runs": [{"slug": "a-bad", "status": "pending"}]}
        out, printed = self._sweep(unclaimable=self._unclaimable_raising_for("a-bad"))
        self.assertEqual(out, [])
        self.assertEqual(self.saves, [])
        self.assertIn("reconcile_queue: a-bad skipped: md boom", printed)

    def test_broken_sweep_scaffold_is_logged_not_swallowed(self):
        broken = mock.patch.object(_worker, "load_queue",
                                   side_effect=RuntimeError("boom"))
        out, printed = self._sweep(load=broken)
        self.assertEqual(out, [])                     # never raises into tick()
        self.assertIn("reconcile_queue: sweep aborted: boom", printed)
        self.assertEqual(self.saves, [])

    def test_clean_entries_have_no_skip_lines(self):
        self._done("a-bad", "b-good")
        self.queue = {"runs": [{"slug": "a-bad", "status": "pending"},
                               {"slug": "b-good", "status": "in_progress"}]}
        out, printed = self._sweep()
        self.assertEqual(out, ["a-bad", "b-good"])
        self.assertEqual([r["status"] for r in self.queue["runs"]],
                         ["completed", "completed"])
        self.assertEqual(len(self.saves), 1)
        self.assertNotIn("skipped:", printed)


if __name__ == "__main__":
    unittest.main()
