"""Tests for the stale-human-task sweep.

The fixture builds its schema with `cli.init_db` rather than a hand-copied
CREATE TABLE: the sweep filters on `is_parent` / `not_before` / `priority` /
`worker_mode`, all of which arrive via migrations, so a hand-rolled schema that
drifts from production would let these tests pass against columns the real DB
does not have. Every test passes an explicit temp path — nothing here may reach
the real vault DB.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from todo_system import cli, ownership
from todo_system.orchestrator import dashboard, stale
from todo_system.orchestrator.assignment import Ledger
from todo_system.orchestrator.registry import Registry

TODAY = "2026-08-18"


def _days_ago(n: int) -> str:
    return (dt.date.fromisoformat(TODAY) - dt.timedelta(days=n)).isoformat()


class StaleTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "todo.db"
        con = cli.db_connect(self.db)
        cli.init_db(con)
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, slug, *, age=30, due=None, not_before=None, worker_mode="manual",
            status="open", is_parent=0, priority=50.0, title=None):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO tasks (slug, title, path, status, due, not_before, updated,"
            " priority, is_parent, worker_mode, imported_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (slug, title or slug.replace("-", " "), f"Todo/items/{slug}.md", status,
             due, not_before, _days_ago(age), priority, is_parent, worker_mode, TODAY),
        )
        con.commit()
        con.close()

    def slugs(self, **kw):
        return [r["slug"] for r in stale.find_stale(self.db, today=TODAY, **kw)]


class FindStaleTest(StaleTestBase):
    def test_plain_stale_task_is_found(self):
        self.add("keystore-upload", age=8)
        self.assertEqual(self.slugs(), ["keystore-upload"])

    def test_future_not_before_excluded(self):
        """Acceptance criterion: a deferred task is deliberately deferred."""
        self.add("deferred", age=40, not_before="2026-09-01")
        self.add("plain", age=40)
        self.assertEqual(self.slugs(), ["plain"])

    def test_past_not_before_included(self):
        """The window has already opened, so the task is genuinely neglected."""
        self.add("window-open", age=40, not_before="2026-08-01")
        self.assertEqual(self.slugs(), ["window-open"])

    def test_not_before_today_included(self):
        """`> today`, not `>= today` — mirrors the worker.py dispatch gate."""
        self.add("starts-today", age=40, not_before=TODAY)
        self.assertEqual(self.slugs(), ["starts-today"])

    def test_not_before_dash_sentinel_is_absent(self):
        """CLAUDE.md writes "no value" as "-"; it must not read as a date."""
        self.add("dash", age=40, not_before="-")
        self.assertEqual(self.slugs(), ["dash"])

    def test_not_before_time_of_day(self):
        """A time-of-day gate holds until that minute, not until tomorrow."""
        self.add("timed-space", age=40, not_before="2026-08-18 17:00")
        self.add("timed-t", age=40, not_before="2026-08-18T17:00")
        self.add("plain", age=40)
        at = dt.datetime.fromisoformat(TODAY)
        self.assertEqual(self.slugs(now=at.replace(hour=16, minute=59)), ["plain"])
        self.assertEqual(sorted(self.slugs(now=at.replace(hour=17))),
                         ["plain", "timed-space", "timed-t"])
        # `today` alone = 00:00 that day, so the 17:00 gate still holds.
        self.assertEqual(self.slugs(), ["plain"])
        # Neither `today` nor `now`: host-local now, no crash.
        stale.find_stale(self.db)

    def test_task_with_due_excluded(self):
        """A deadline means the brief's 마감 임박 sections already carry it."""
        self.add("has-due", age=40, due="2026-12-01")
        self.add("dash-due", age=40, due="-")       # "-" == undated
        self.add("blank-due", age=40, due="")
        self.assertEqual(sorted(self.slugs()), ["blank-due", "dash-due"])

    def test_worker_owned_excluded_matching_ownership_module(self):
        """Parity with `ownership.is_worker_owned` — not a re-implementation."""
        modes = ["auto", "blocked", "delegate", "review", "manual", "off", "", "-", "  ", None]
        for i, mode in enumerate(modes):
            self.add(f"m{i:02d}", age=40, worker_mode=mode)
        found = set(self.slugs())
        expected = {f"m{i:02d}" for i, mode in enumerate(modes)
                    if not ownership.is_worker_owned(mode)}
        self.assertEqual(found, expected)
        self.assertIn("m01", {f"m{i:02d}" for i, m in enumerate(modes) if m == "blocked"})
        self.assertNotIn("m01", found)              # `blocked` is the pipeline's

    def test_closed_and_parent_excluded(self):
        self.add("done-task", age=40, status="done")
        self.add("cancelled-task", age=40, status="cancelled")
        self.add("parent-task", age=40, is_parent=1)
        self.add("leaf", age=40)
        self.assertEqual(self.slugs(), ["leaf"])

    def test_threshold_boundary(self):
        self.add("exactly-n", age=stale.STALE_DAYS)
        self.add("one-short", age=stale.STALE_DAYS - 1)
        self.assertEqual(self.slugs(), ["exactly-n"])

    def test_threshold_is_overridable(self):
        self.add("three-days", age=3)
        self.assertEqual(self.slugs(), [])
        self.assertEqual(self.slugs(stale_days=3), ["three-days"])

    def test_unparseable_updated_skipped(self):
        """No computable age ⇒ no claim about how stale it is."""
        self.add("ok", age=40)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tasks (slug, title, path, status, updated, worker_mode,"
                    " is_parent, imported_at) VALUES ('junk','Junk','p','open',?,'manual',0,?)",
                    ("not-a-date", TODAY))
        con.execute("INSERT INTO tasks (slug, title, path, status, updated, worker_mode,"
                    " is_parent, imported_at) VALUES ('null-upd','Null','p','open',NULL,"
                    "'manual',0,?)", (TODAY,))
        con.commit()
        con.close()
        self.assertEqual(self.slugs(), ["ok"])

    def test_sort_priority_then_age(self):
        self.add("low-old", age=60, priority=10.0)
        self.add("high-new", age=10, priority=90.0)
        self.add("mid-old", age=50, priority=50.0)
        self.add("mid-new", age=20, priority=50.0)
        self.assertEqual(self.slugs(), ["high-new", "mid-old", "mid-new", "low-old"])

    def test_null_priority_sorts_as_50(self):
        """Same as `cli.py`'s `COALESCE(priority, 50) DESC` — not as 0."""
        self.add("null-prio", age=10, priority=None)
        self.add("p60", age=10, priority=60.0)
        self.add("p40", age=10, priority=40.0)
        self.assertEqual(self.slugs(), ["p60", "null-prio", "p40"])

    def test_read_only_connection_rejects_writes(self):
        """The sweep must never be able to mutate task state."""
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("UPDATE tasks SET status='done'")
        con.close()

    def test_sweep_does_not_mutate_the_db(self):
        self.add("untouched", age=40, due=None)
        before = self.db.read_bytes()
        stale.find_stale(self.db, today=TODAY)
        self.assertEqual(before, self.db.read_bytes())


class RenderSectionTest(StaleTestBase):
    def test_empty_renders_none(self):
        self.assertEqual(stale.render_section([]), ["- 없음"])

    def test_cap_and_truncation_count(self):
        for i in range(8):
            self.add(f"t{i}", age=10 + i)
        rows = stale.find_stale(self.db, today=TODAY)
        self.assertEqual(len(rows), 8)
        lines = stale.render_section(rows, limit=5)
        self.assertEqual(len(lines), 6)                      # 5 shown + 1 tail
        self.assertIn("외 3건 더", lines[-1])
        self.assertIn("총 8건", lines[-1])                    # total, not just the cut

    def test_no_tail_when_under_cap(self):
        for i in range(3):
            self.add(f"t{i}", age=10 + i)
        lines = stale.render_section(stale.find_stale(self.db, today=TODAY), limit=5)
        self.assertEqual(len(lines), 3)
        self.assertNotIn("외", "".join(lines))

    def test_line_carries_slug_age_and_priority(self):
        self.add("keystore-upload", age=8, priority=59.0, title="키스토어 업로드")
        [line] = stale.render_section(stale.find_stale(self.db, today=TODAY))
        self.assertIn("`keystore-upload`", line)
        self.assertIn("키스토어 업로드", line)
        self.assertIn("8일 방치", line)
        self.assertIn("59", line)                            # 59.0 → "59"
        self.assertIn(_days_ago(8), line)


class DashboardSectionTest(StaleTestBase):
    def setUp(self):
        super().setUp()
        self.led = Ledger(self.tmp / "assignments")
        self.reg = Registry([])

    def test_dashboard_section_rendered(self):
        self.add("keystore-upload", age=40)
        md = dashboard.render(self.led, self.reg, self.db)
        self.assertIn("## 🕰 방치된 사람 태스크", md)
        self.assertIn("keystore-upload", md)

    def test_section_precedes_completed_list(self):
        """The brief is told to stop at ✅ 완료, so order is load-bearing."""
        self.add("keystore-upload", age=40)
        md = dashboard.render(self.led, self.reg, self.db)
        self.assertLess(md.index("🕰 방치된 사람 태스크"), md.index("## ✅ 완료"))

    def test_db_path_none_is_not_an_error(self):
        md = dashboard.render(self.led, self.reg)
        self.assertIn("## 🕰 방치된 사람 태스크", md)
        self.assertNotIn("스윕 실패", md)

    def test_broken_db_fails_soft_but_visibly(self):
        """A dead DB must not kill the 5-minute tick — nor masquerade as 없음."""
        md = dashboard.render(self.led, self.reg, self.tmp / "nope.db")
        self.assertIn("스윕 실패", md)
        self.assertIn("## ✅ 완료", md)                       # rest still rendered

    def test_write_passes_db_through(self):
        self.add("keystore-upload", age=40)
        out = self.tmp / "Orchestrator.md"
        dashboard.write(self.led, self.reg, str(out), self.db)
        self.assertIn("keystore-upload", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
