"""AC5: reconcile_parents auto-closes an auto-rescoped bundle once every child is
done, while an ordinary all-children-done parent is still only surfaced to a human.

Uses a real tmp vault + todo.db (import-md) because parents_needing_review reads the
DB (is_parent / parent_slug / child status). The `cli` globals are rebound to the
tmp vault on top of the package conftest's runtime-path isolation so nothing touches
the live vault.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from todo_system import cli
from todo_system import worker
from todo_system.orchestrator.assignment import Ledger
from todo_system.orchestrator.orchestrator import Orchestrator
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.registry import Registry


def _parent_md(items: Path, slug: str, *, auto_rescoped: bool) -> Path:
    extra = "auto_rescoped: true\n" if auto_rescoped else ""
    (items / f"{slug}.md").write_text(
        f"---\ntags:\n  - todo\n  - worker\nstatus: open\nworker_mode: off\n"
        f"is_parent: true\n{extra}outcome: 묶음\n---\n\n# {slug}\n\n"
        f"## Progress Log\n\n- 2026-10-10: 생성\n",
        encoding="utf-8")
    return items / f"{slug}.md"


def _child_md(items: Path, slug: str, parent: str) -> None:
    (items / f"{slug}.md").write_text(
        f"---\ntags:\n  - todo\nstatus: done\nparent: \"[[{parent}]]\"\n"
        f"worker_mode: auto\noutcome: 하위\n---\n\n# {slug}\n\n- [x] done\n",
        encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "vault"
    items = root / "Todo" / "items"
    items.mkdir(parents=True)
    (root / "Worker" / "assignments").mkdir(parents=True)
    (root / "Worker" / "_state").mkdir(parents=True)
    db = root / "Todo" / "todo.db"
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "TODO_DIR", root / "Todo")
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    monkeypatch.setattr(cli, "DB_PATH", db)
    return root, items, db


def _import(db: Path):
    con = cli.db_connect(db)
    cli.init_db(con)
    cli.import_markdown_to_db(con)
    con.close()


def _status(path: Path) -> str:
    return worker.parse_frontmatter(path.read_text(encoding="utf-8"))[0]["status"]


def test_auto_rescoped_bundle_is_closed_ordinary_parent_is_only_surfaced(vault):
    root, items, db = vault
    # a bundle we auto-created (marked) + 3 finished children
    bundle = _parent_md(items, "auto-bundle", auto_rescoped=True)
    for i in range(3):
        _child_md(items, f"auto-bundle-part-{i}", "auto-bundle")
    # an ordinary parent (no mark) + 2 finished children — human judgement to close
    ordinary = _parent_md(items, "plain-parent", auto_rescoped=False)
    for i in range(2):
        _child_md(items, f"plain-parent-child-{i}", "plain-parent")
    _import(db)

    sent: list = []
    orch = Orchestrator(FileMailbox(root / "Worker" / "mb"), Registry([]),
                        Ledger(root / "Worker" / "assignments"),
                        notify=lambda t, link=None: sent.append(t),
                        alert_route=lambda text, **kw: ("send", text))

    processed = orch.reconcile_parents()

    assert "auto-bundle" in processed
    assert _status(bundle) == "done"           # auto-closed (AC5)
    assert _status(ordinary) == "open"         # ordinary parent left for a human
    assert len(sent) == 1                       # one 🧭 surface ping, for the ordinary parent only
    assert "plain-parent" in sent[0]
    assert "auto-bundle" not in sent[0]


def test_auto_close_is_idempotent(vault):
    root, items, db = vault
    bundle = _parent_md(items, "auto-bundle", auto_rescoped=True)
    for i in range(2):
        _child_md(items, f"auto-bundle-part-{i}", "auto-bundle")
    _import(db)

    orch = Orchestrator(FileMailbox(root / "Worker" / "mb"), Registry([]),
                        Ledger(root / "Worker" / "assignments"),
                        notify=lambda t, link=None: None,
                        alert_route=lambda text, **kw: ("send", text))

    assert "auto-bundle" in orch.reconcile_parents()
    assert _status(bundle) == "done"
    # second pass: the bundle is no longer open, so nothing to do
    assert orch.reconcile_parents() == []
