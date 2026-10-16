"""Tests for the CLI commands that write vault files: ``new``, ``now`` and
``focus``.

These commands write through module globals computed once at import, so a test
that only passes ``--db`` still writes into whichever checkout it runs in — in
the live vault that overwrites the real ``Todo/NOW.md``. The ``vault`` fixture
rebinds every global the three commands touch into a tmp vault:

* ``ROOT`` — ``parse_task_file`` calls ``path.relative_to(ROOT)``, and the
  dashboard reads ``ROOT/Worker/queue.json``. A tmp ROOT without that file makes
  the dashboard print "Worker 큐 파일이 아직 없습니다", which is the positive
  evidence that the redirect took.
* ``TODO_DIR`` — templates for ``new`` and the ``Todo.md`` lane index.
* ``ITEMS_DIR`` — ``new`` writes here, ``focus`` imports from here.
* ``NOW_PATH`` / ``FOCUS_PATH`` / ``DASHBOARD_PATH`` — the three sync_* writers.
* ``DB_PATH`` — conftest already redirects it; the tests pass ``--db`` anyway.

The three notes and ``DB_PATH`` are package-wide ``_RUNTIME_PATHS`` rows: the
conftest redirects them into its own tmp dir per test, and its tripwire refuses
and fails the session on any write to the live files. ``vault`` still rebinds
the notes, on top of that redirect, into its tmp vault so that ``now``/``focus``
write where the tests read them back (``vault.now`` etc.); those read-backs are
the evidence that the two layers agree on one file.

The conftest does not cover the real ``Todo/items/``, so this file guards it
itself: each ``new`` target is put under ``runtime_tripwire.guarding`` (a write
is refused and recorded), and teardown asserts no hits and an unchanged
existence/mtime/listing of the real directory.

``focus`` runs ``import_markdown_to_db`` first, which DELETEs every DB row that
has no markdown file, so ``focus`` tests seed md files, not DB rows. Commands
run in-process only: a subprocess would not see the monkeypatched globals and
would write the real files.

Run in the live vault, a sync or capture that adds a real task file inside a
test's few-millisecond window fails the listing check without any leak. The
tripwire hits are the deterministic signal; worktrees have no daemon.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system import cli, paths

PAST = "2000-01-01"
FUTURE = "2999-12-31"

_REDIRECTED = ("ROOT", "TODO_DIR", "ITEMS_DIR", "NOW_PATH", "FOCUS_PATH", "DASHBOARD_PATH")
# Captured at import, before any fixture can rebind them — the same reason
# conftest captures _REAL_RUNTIME at import.
_LIVE = {name: getattr(cli, name) for name in _REDIRECTED}

# Every slug a test passes to ``new``; each one's real items path is guarded.
NEW_SLUGS = ("write-cmd-alpha", "write-cmd-beta", "write-cmd-delta", "write-cmd-gamma")


def _live_items_snapshot() -> tuple:
    items = _LIVE["ITEMS_DIR"]
    if items.is_dir():
        return (True, items.stat().st_mtime_ns, tuple(sorted(os.listdir(items))))
    return (False, None, ())


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_tripwire):
    root = tmp_path / "vault"
    todo = root / "Todo"
    items = todo / "items"
    items.mkdir(parents=True)
    # Source templates from the resolved vault root, not parents[2]/Todo: a split
    # engine tree keeps templates under the vault (MACP_VAULT_ROOT → vault-template/
    # Todo/templates), not beside the engine package (public-split-10 §11.7 ③).
    templates = paths.vault_root() / "Todo" / "templates"
    assert templates.is_dir(), templates
    shutil.copytree(templates, todo / "templates")

    # The conftest redirect sits underneath: the notes are off the live files
    # before this fixture rebinds them again.
    for name in ("NOW_PATH", "FOCUS_PATH", "DASHBOARD_PATH"):
        assert getattr(cli, name) != _LIVE[name], name

    before = _live_items_snapshot()
    guarded = [_LIVE["ITEMS_DIR"] / f"{slug}.md" for slug in NEW_SLUGS]
    hits: dict[str, list] = {}
    with contextlib.ExitStack() as stack:
        for path in guarded:
            hits[str(path)] = stack.enter_context(runtime_tripwire.guarding(path))

        tmp = {
            "ROOT": root,
            "TODO_DIR": todo,
            "ITEMS_DIR": items,
            "NOW_PATH": todo / "NOW.md",
            "FOCUS_PATH": todo / "FOCUS.md",
            "DASHBOARD_PATH": todo / "Dashboard.md",
        }
        for name, value in tmp.items():
            monkeypatch.setattr(cli, name, value)
            # Guards a capture-order regression: _LIVE must hold the real value.
            assert value != _LIVE[name], name

        db = todo / "todo.db"
        con = cli.db_connect(db)
        cli.init_db(con)
        con.close()

        yield SimpleNamespace(
            root=root,
            todo=todo,
            items=items,
            now=tmp["NOW_PATH"],
            focus=tmp["FOCUS_PATH"],
            dashboard=tmp["DASHBOARD_PATH"],
            db=db,
        )

    # The stack is closed first so a failing assert cannot leave paths guarded
    # for later tests; the hit lists stay ours after the guards are released.
    touched = {path: h for path, h in hits.items() if h}
    assert not touched, f"write command reached the live vault: {touched}"
    after = _live_items_snapshot()
    assert after == before, f"live Todo/items/ changed: {before!r} -> {after!r}"


def _run(db: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    args = cli.build_parser().parse_args(["--db", str(db), *argv])
    rc = args.func(args)
    return rc, capsys.readouterr().out


def _seed(db: Path, *rows: dict) -> None:
    con = sqlite3.connect(str(db))
    for row in rows:
        values = {
            "path": f"Todo/items/{row['slug']}.md",
            "imported_at": "2026-01-01T00:00:00",
            "status": "open",
            **row,
        }
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        con.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(values.values()))
    con.commit()
    con.close()


def _write_md(items: Path, slug: str, *, title: str, **fm: str) -> None:
    # Plain unquoted values only, no inline comments; "no due" is a missing key.
    lines = ["---", *(f"{key}: {value}" for key, value in fm.items()), "---", "",
             f"# {title}", "", "- [ ] step one", ""]
    (items / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")


def _section(md: str, heading: str) -> str:
    """From ``heading`` up to the next ``## `` heading (or the end)."""
    start = md.index(heading)
    end = md.find("\n## ", start + len(heading))
    return md[start:] if end == -1 else md[start:end]


def _db_slugs(db: Path) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {row[0] for row in con.execute("SELECT slug FROM tasks")}
    finally:
        con.close()


# ---------------------------------------------------------------- new


def test_new_scaffolds_from_template(vault, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(vault.db, capsys, "new", "write-cmd-alpha", "--title", "Alpha Title")

    dest = vault.items / "write-cmd-alpha.md"
    assert rc == 0
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert f"updated: {dt.date.today().isoformat()}" in content
    assert 'due: "-"' in content
    assert 'worker_branch: "worker/write-cmd-alpha"' in content
    assert "\n# Alpha Title\n" in content
    # The scaffold stays undispatchable until a person switches it to auto.
    assert "\nworker_mode: blocked\n" in content
    # The template's own date survives in its Progress Log, so only the
    # frontmatter line is checked for the replacement.
    assert "updated: 2026-05-19" not in content
    assert "due: YYYY-MM-DD" not in content
    assert "worker/<slug>" not in content
    assert "# 작업 제목" not in content
    assert f"created {dest}" in out
    assert "Fill in outcome" in out
    assert os.listdir(vault.items) == ["write-cmd-alpha.md"]


def test_new_title_defaults_to_slug(vault, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(vault.db, capsys, "new", "write-cmd-beta")

    assert rc == 0
    content = (vault.items / "write-cmd-beta.md").read_text(encoding="utf-8")
    assert "\n# write-cmd-beta\n" in content


def test_new_unknown_template_returns_1(vault, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(vault.db, capsys, "new", "write-cmd-delta", "--template", "no-such")

    assert rc == 1
    assert "no template 'no-such'; available:" in out
    assert "auto-worker" in out
    assert os.listdir(vault.items) == []


def test_new_existing_dest_returns_1(vault, capsys: pytest.CaptureFixture[str]) -> None:
    dest = vault.items / "write-cmd-delta.md"
    dest.write_text("sentinel\n", encoding="utf-8")

    rc, out = _run(vault.db, capsys, "new", "write-cmd-delta")

    assert rc == 1
    assert f"already exists: {dest}" in out
    assert dest.read_text(encoding="utf-8") == "sentinel\n"


# ---------------------------------------------------------------- now

OVERDUE = "## 🚨 Overdue"
RECENT = "## 🔵 Recently Updated"
WORKER = "## 🤖 Worker 대기열"


def test_now_empty_db_writes_all_three_notes(vault, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(vault.db, capsys, "now")

    assert rc == 0
    assert f"updated now file: {vault.now}" in out
    for path in (vault.now, vault.focus, vault.dashboard):
        assert path.exists(), path
    assert "실행할 open task가 없습니다" in vault.now.read_text(encoding="utf-8")
    dashboard = vault.dashboard.read_text(encoding="utf-8")
    # No Worker/queue.json under the tmp ROOT: proof the dashboard read it there.
    assert "Worker 큐 파일이 아직 없습니다" in dashboard
    assert "Calendar 연동 미설정" in dashboard


def test_now_picks_the_human_task(vault, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(
        vault.db,
        {"slug": "human-now", "title": "Human now task", "due": PAST, "worker_mode": "manual",
         "next_action": "do the human thing"},
        {"slug": "worker-now", "title": "Worker now task", "due": PAST, "worker_mode": "auto",
         "priority": 99},
    )

    rc, out = _run(vault.db, capsys, "now")

    assert rc == 0
    now = vault.now.read_text(encoding="utf-8")
    assert "[[Todo/items/human-now|Human now task]]" in now
    assert "do the human thing" in now
    assert "(남은 subtask 없음)" in now
    assert "Worker now task" not in now
    focus = vault.focus.read_text(encoding="utf-8")
    assert "human-now" in _section(focus, OVERDUE)
    assert "worker-now" not in _section(focus, OVERDUE)
    assert "worker-now" in _section(focus, WORKER)
    assert "Human now task" in out


@pytest.mark.parametrize("nb", ["2026-10-12 17:00", "2026-10-12T17:00"])
def test_now_pick_honors_not_before_time(vault, nb: str) -> None:
    # The SQL used `not_before <= today`, which kept a time value out all day.
    _seed(
        vault.db,
        {"slug": "timed", "title": "Timed", "worker_mode": "manual", "priority": 90,
         "not_before": nb},
        {"slug": "plain", "title": "Plain", "worker_mode": "manual", "priority": 10},
    )
    con = cli.db_connect(vault.db)
    try:
        before = cli.pick_now_task_sqlite(con, now=dt.datetime(2026, 10, 12, 16, 59))
        at = cli.pick_now_task_sqlite(con, now=dt.datetime(2026, 10, 12, 17, 0))
    finally:
        con.close()
    assert before["slug"] == "plain"
    assert at["slug"] == "timed"


def test_now_pick_date_only_not_before_unchanged(vault) -> None:
    _seed(
        vault.db,
        {"slug": "future", "title": "Future", "worker_mode": "manual", "priority": 90,
         "not_before": FUTURE},
        {"slug": "past", "title": "Past", "worker_mode": "manual", "priority": 50,
         "not_before": PAST},
        {"slug": "dash", "title": "Dash", "worker_mode": "manual", "priority": 10,
         "not_before": "-"},
    )
    con = cli.db_connect(vault.db)
    try:
        assert cli.pick_now_task_sqlite(con)["slug"] == "past"
        con.execute("UPDATE tasks SET status='done' WHERE slug='past'")
        assert cli.pick_now_task_sqlite(con)["slug"] == "dash"
        con.execute("UPDATE tasks SET status='done' WHERE slug='dash'")
        assert cli.pick_now_task_sqlite(con) is None
    finally:
        con.close()


# ---------------------------------------------------------------- set


def test_set_not_before_time_is_quoted_and_round_trips(
    vault, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_md(vault.items, "write-cmd-set", title="Set target", worker_mode="manual",
              not_before=PAST)
    _seed(vault.db, {"slug": "write-cmd-set", "title": "Set target"})

    rc, _ = _run(vault.db, capsys, "set", "write-cmd-set", "--not-before", "2026-10-12 17:00")

    assert rc == 0
    path = vault.items / "write-cmd-set.md"
    content = path.read_text(encoding="utf-8")
    assert '\nnot_before: "2026-10-12 17:00"\n' in content
    assert cli.parse_task_file(path).not_before == "2026-10-12 17:00"

    rc, _ = _run(vault.db, capsys, "set", "write-cmd-set", "--not-before", "2026-10-13")
    assert rc == 0
    assert "\nnot_before: 2026-10-13\n" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- focus


def test_focus_imports_md_then_renders(vault, capsys: pytest.CaptureFixture[str]) -> None:
    _write_md(vault.items, "overdue-human", title="Overdue human task", due=PAST,
              worker_mode="manual", next_action="clear the overdue")
    _write_md(vault.items, "far-human", title="Far human task", due=FUTURE,
              worker_mode="manual")
    _write_md(vault.items, "undated-human", title="Undated human task",
              updated=dt.date.today().isoformat(), worker_mode="manual")
    _write_md(vault.items, "worker-auto", title="Worker auto task", due=PAST,
              worker_mode="auto")
    # A row with no md file: import_markdown_to_db deletes it.
    _seed(vault.db, {"slug": "stale-row", "title": "Stale row"})

    rc, out = _run(vault.db, capsys, "focus")

    assert rc == 0
    assert f"updated focus file: {vault.focus}" in out
    focus = vault.focus.read_text(encoding="utf-8")
    assert "Overdue human task" in _section(focus, OVERDUE)
    assert "clear the overdue" in _section(focus, OVERDUE)
    # FUTURE is past the 7-day horizon and has a due date, so it is nowhere.
    assert "Far human task" not in focus
    assert "Undated human task" in _section(focus, RECENT)
    assert "Worker auto task" in _section(focus, WORKER)
    assert "Worker auto task" not in _section(focus, OVERDUE)
    now = vault.now.read_text(encoding="utf-8")
    assert "[[Todo/items/overdue-human|Overdue human task]]" in now
    assert "step one" in now
    assert vault.dashboard.exists()
    assert _db_slugs(vault.db) == {"overdue-human", "far-human", "undated-human", "worker-auto"}
    con = sqlite3.connect(str(vault.db))
    try:
        (path,) = con.execute(
            "SELECT path FROM tasks WHERE slug = 'overdue-human'").fetchone()
    finally:
        con.close()
    assert path == "Todo/items/overdue-human.md"


def test_focus_days_argument(vault, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = _run(vault.db, capsys, "focus", "--days", "1", "--recent-days", "3")

    assert rc == 0
    focus = vault.focus.read_text(encoding="utf-8")
    assert "## 🟡 Next 1 Days" in focus
    assert "Recently Updated (No Due, 3d)" in focus
    assert "## 🟡 Next 1 Days" in out


# ---------------------------------------------------------------- live vault


def test_write_commands_leave_live_vault_untouched(
    vault, capsys: pytest.CaptureFixture[str], runtime_tripwire
) -> None:
    before = _live_items_snapshot()
    before_hits = len(runtime_tripwire.hits)

    assert _run(vault.db, capsys, "new", "write-cmd-gamma")[0] == 0
    assert _run(vault.db, capsys, "now")[0] == 0
    assert _run(vault.db, capsys, "focus")[0] == 0

    # The live notes are conftest's: no session-level tripwire record.
    assert len(runtime_tripwire.hits) == before_hits
    after = _live_items_snapshot()
    assert after == before, f"live Todo/items/ changed: {before!r} -> {after!r}"
    assert not (_LIVE["ITEMS_DIR"] / "write-cmd-gamma.md").exists()
    # Positive evidence: every write landed in the tmp vault instead.
    for path in (vault.now, vault.focus, vault.dashboard, vault.items / "write-cmd-gamma.md"):
        assert path.exists(), path
    assert "write-cmd-gamma" in _db_slugs(vault.db)
