#!/usr/bin/env python3
"""Export local task data to a JSON snapshot for brief agent consumption.

Reads the local SQLite source of truth (Todo/todo.db) and writes
Todo/Snapshots/latest.json with a summary of:
- Open tasks (top 20 by priority)
- Overdue tasks
- Tasks due today
- Upcoming tasks (next 7 days)
- Recently updated tasks (no due, last 14 days)
- Worker queue status

The brief agent reads this snapshot instead of querying the DB directly,
keeping the read path simple and offline.

Usage:
    uv run python scripts/export_db_snapshot.py
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

_INSTALL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_INSTALL_DIR))          # bootstrap — raw install location
from todo_system import paths  # noqa: E402
# Engine root through the resolver: dev checkout -> repo root (same value as the
# bootstrap above); wheel install -> todo_system/_engine, where scripts/ and
# .claude/commands ship in repo layout. The bootstrap only puts the engine on
# sys.path; this is the definition the rest of the module uses.
_INSTALL_DIR = paths.engine_root()
# Data root through the resolver: honours MACP_VAULT_ROOT / .macp/config.toml.
# The start= anchor is paths.vault_search_start(): the engine root in a checkout
# (byte-identical to the former Path(__file__).resolve().parents[1] when unset),
# None in an installed wheel so the cwd-upward search runs instead of anchoring
# on site-packages, which never holds a vault marker.
VAULT_ROOT = paths.vault_root(start=paths.vault_search_start())
DB_PATH = VAULT_ROOT / "Todo" / "todo.db"
SNAPSHOT_DIR = VAULT_ROOT / "Todo" / "Snapshots"
SNAPSHOT_PATH = SNAPSHOT_DIR / "latest.json"


def _norm_due(value: str | None) -> str | None:
    """Normalize a due value to a real date string or None.

    The vault frontmatter convention stores "no due date" as the literal
    string "-" (see CLAUDE.md), so treat "-" and empty as undated.
    """
    if not value or value == "-":
        return None
    return value


def export_snapshot() -> dict:
    """Read open tasks from the local SQLite DB and build the snapshot dict."""
    today = dt.date.today()
    horizon = today + dt.timedelta(days=7)
    recent_cutoff = today - dt.timedelta(days=14)

    # Fetch all open tasks from the local source of truth
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        tasks = con.execute(
            "SELECT slug, title, due, priority, status, next_action, outcome, "
            "lane, total_subtasks, done_subtasks, worker_mode, updated "
            "FROM tasks WHERE status = 'open'"
        ).fetchall()
    finally:
        con.close()

    # Categorize tasks
    overdue = []
    today_items = []
    upcoming = []
    recently_updated_undated = []
    top_priority = []

    for t in tasks:
        due = _norm_due(t["due"])
        task_data = {
            "slug": t["slug"],
            "title": t["title"],
            "area": None,
            "due": due,
            "priority": t["priority"],
            "status": t["status"],
            "next_action": t["next_action"],
            "outcome": t["outcome"],
            "lane": t["lane"],
            "total_subtasks": t["total_subtasks"],
            "done_subtasks": t["done_subtasks"],
            "worker_mode": t["worker_mode"],
        }

        if due:
            try:
                due_date = dt.date.fromisoformat(due[:10])
                if due_date < today:
                    overdue.append(task_data)
                elif due_date == today:
                    today_items.append(task_data)
                elif due_date <= horizon:
                    upcoming.append(task_data)
            except ValueError:
                pass
        else:
            # No due date — check if recently updated
            if t["updated"]:
                try:
                    updated_date = dt.date.fromisoformat(t["updated"][:10])
                    if updated_date >= recent_cutoff:
                        recently_updated_undated.append(task_data)
                except ValueError:
                    pass

    # Top 20 by priority
    sorted_tasks = sorted(tasks, key=lambda x: -(x["priority"] or 0))
    for t in sorted_tasks[:20]:
        top_priority.append({
            "slug": t["slug"],
            "title": t["title"],
            "area": None,
            "due": _norm_due(t["due"]),
            "priority": t["priority"],
            "next_action": t["next_action"],
            "outcome": t["outcome"],
            "lane": t["lane"],
        })

    # Read worker queue
    queue_path = VAULT_ROOT / "Worker" / "queue.json"
    worker_queue = {"runs": []}
    if queue_path.exists():
        try:
            worker_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    queue_summary = {
        "in_progress": [r for r in worker_queue.get("runs", []) if r.get("status") == "in_progress"],
        "pending": [r for r in worker_queue.get("runs", []) if r.get("status") == "pending"],
        "completed_24h": [],
        "failed": [r for r in worker_queue.get("runs", []) if r.get("status") == "failed"],
    }

    # Recent completed (last 24h)
    cutoff_24h = (dt.datetime.now() - dt.timedelta(hours=24)).isoformat()
    for r in worker_queue.get("runs", []):
        if r.get("status") == "completed" and (r.get("finished_at") or "") >= cutoff_24h:
            queue_summary["completed_24h"].append(r)

    snapshot = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "sqlite",
        "stats": {
            "total_open": len(tasks),
            "overdue": len(overdue),
            "today": len(today_items),
            "upcoming_7d": len(upcoming),
            "recently_updated_undated": len(recently_updated_undated),
        },
        "top_priority": top_priority,
        "overdue": overdue,
        "today": today_items,
        "upcoming": upcoming,
        "recently_updated_undated": recently_updated_undated,
        "worker_queue": queue_summary,
    }

    return snapshot


def main() -> int:
    # Local SQLite DB is the source of truth; skip cleanly if it is absent.
    if not DB_PATH.exists():
        print(f"export_db_snapshot: {DB_PATH.relative_to(VAULT_ROOT)} not found — skipping")
        return 0

    snapshot = export_snapshot()

    # Write snapshot
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"export_db_snapshot: written to {SNAPSHOT_PATH.relative_to(VAULT_ROOT)}")
    print(f"  stats: {snapshot['stats']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
