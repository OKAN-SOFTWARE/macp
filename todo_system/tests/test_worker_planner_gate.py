"""Pins the planner skip-gate + queue I/O + frontmatter parser contracts in
`todo_system/worker.py`: `compute_todo_state_hash`, `load_planner_state`,
`is_planner_exhausted`, `count_auto_pending`, `parse_frontmatter`, and
`load_queue`/`save_queue`/`queue_lock`. The sibling state-writer function is
deliberately out of scope — no call, no assertion (see the originating task's
acceptance criteria #14).

This repo's `todo_system/tests/conftest.py` only isolates `graph_memory` /
`session_log` globals (`isolated_vault`), not `worker`'s. There is no
`_runtime_paths_isolated` autouse fixture in this codebase, so every test here
redirects the five relevant `worker` module globals itself via
`mock.patch.object(worker, ...)`, scoped to a fresh `tmp_path` per test.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from unittest import mock

import pytest

from todo_system import worker


@pytest.fixture
def isolated_paths(tmp_path):
    """Redirect worker's items/queue/planner-state/lock/logs globals into tmp_path."""
    items_dir = tmp_path / "items"
    queue_path = tmp_path / "queue.json"
    planner_state_path = tmp_path / "planner_state.json"
    queue_lock_path = tmp_path / "queue.lock"
    logs_dir = tmp_path / "logs"
    with (
        mock.patch.object(worker, "ITEMS_DIR", items_dir),
        mock.patch.object(worker, "QUEUE_PATH", queue_path),
        mock.patch.object(worker, "PLANNER_STATE_PATH", planner_state_path),
        mock.patch.object(worker, "QUEUE_LOCK_PATH", queue_lock_path),
        mock.patch.object(worker, "LOGS_DIR", logs_dir),
    ):
        yield tmp_path


def _write_item(items_dir, name: str, content: str) -> None:
    items_dir.mkdir(parents=True, exist_ok=True)
    (items_dir / name).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# compute_todo_state_hash (AC2, AC3, AC4)
# ---------------------------------------------------------------------------


class TestComputeTodoStateHash:
    def test_reproducible_for_unchanged_state(self, isolated_paths):
        _write_item(isolated_paths / "items", "a.md", "hello world")
        h1 = worker.compute_todo_state_hash(queue={"runs": []})
        h2 = worker.compute_todo_state_hash(queue={"runs": []})
        assert h1 == h2

    def test_changes_when_first_500_chars_change(self, isolated_paths):
        items_dir = isolated_paths / "items"
        _write_item(items_dir, "a.md", "hello world")
        h1 = worker.compute_todo_state_hash(queue={"runs": []})
        _write_item(items_dir, "a.md", "goodbye world")
        h2 = worker.compute_todo_state_hash(queue={"runs": []})
        assert h1 != h2

    def test_unchanged_when_only_tail_beyond_500_changes_and_mtime_restored(self, isolated_paths):
        items_dir = isolated_paths / "items"
        original = ("x" * 500) + "TAIL-ORIGINAL"
        path = items_dir / "a.md"
        _write_item(items_dir, "a.md", original)
        original_stat = path.stat()
        h1 = worker.compute_todo_state_hash(queue={"runs": []})

        path.write_text(("x" * 500) + "TAIL-CHANGED-DIFFERENT-LENGTH-TOO", encoding="utf-8")
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        h2 = worker.compute_todo_state_hash(queue={"runs": []})

        assert h1 == h2

    def test_changes_only_when_runs_change(self, isolated_paths):
        _write_item(isolated_paths / "items", "a.md", "hello")
        queue_a = {"runs": [{"run_id": "1"}], "other_key": "x"}
        queue_b = {"runs": [{"run_id": "1"}], "other_key": "y"}
        queue_c = {"runs": [{"run_id": "2"}], "other_key": "x"}

        h_a = worker.compute_todo_state_hash(queue=queue_a)
        h_b = worker.compute_todo_state_hash(queue=queue_b)
        h_c = worker.compute_todo_state_hash(queue=queue_c)

        assert h_a == h_b
        assert h_a != h_c

    def test_queue_none_falls_back_to_load_queue(self, isolated_paths):
        _write_item(isolated_paths / "items", "a.md", "hello")
        runs = {"runs": [{"run_id": "1"}]}
        worker.save_queue(runs)

        h_via_fallback = worker.compute_todo_state_hash()
        h_via_explicit = worker.compute_todo_state_hash(queue=runs)

        assert h_via_fallback == h_via_explicit

    def test_missing_items_dir_no_exception(self, isolated_paths):
        # ITEMS_DIR points at tmp_path / "items", never created here.
        result = worker.compute_todo_state_hash(queue={"runs": []})
        assert isinstance(result, str)
        assert result

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores chmod 0o000")
    def test_tolerates_one_unreadable_file(self, isolated_paths):
        items_dir = isolated_paths / "items"
        _write_item(items_dir, "a.md", "readable content")
        unreadable = items_dir / "b.md"
        _write_item(items_dir, "b.md", "unreadable content")
        unreadable.chmod(0o000)
        try:
            result = worker.compute_todo_state_hash(queue={"runs": []})
            assert isinstance(result, str)

            # Baseline: same hash with only the readable file present.
            unreadable.unlink()
            baseline = worker.compute_todo_state_hash(queue={"runs": []})
            assert result == baseline
        finally:
            if unreadable.exists():
                unreadable.chmod(0o644)


# ---------------------------------------------------------------------------
# load_planner_state (AC5)
# ---------------------------------------------------------------------------


class TestLoadPlannerState:
    def test_missing_file_returns_empty_dict(self, isolated_paths):
        assert worker.load_planner_state() == {}

    def test_corrupt_json_returns_empty_dict(self, isolated_paths):
        path = isolated_paths / "planner_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        assert worker.load_planner_state() == {}

    def test_valid_json_preserves_keys(self, isolated_paths):
        path = isolated_paths / "planner_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "verdict": "exhausted",
            "todo_state_hash": "abc123",
            "last_run": "2026-09-28T00:00:00",
            "extra_key": 1,
        }
        path.write_text(json.dumps(state), encoding="utf-8")
        assert worker.load_planner_state() == state


# ---------------------------------------------------------------------------
# is_planner_exhausted (AC6, AC7)
# ---------------------------------------------------------------------------


def _write_planner_state(isolated_paths, state: dict) -> None:
    path = isolated_paths / "planner_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


class TestIsPlannerExhausted:
    def test_no_prior_state(self, isolated_paths):
        exhausted, reason = worker.is_planner_exhausted(queue={"runs": []})
        assert exhausted is False
        assert reason == "no prior state"

    def test_verdict_not_exhausted(self, isolated_paths):
        _write_planner_state(isolated_paths, {"verdict": "has_tasks"})
        exhausted, reason = worker.is_planner_exhausted(queue={"runs": []})
        assert exhausted is False
        assert "has_tasks" in reason

    def test_hash_changed(self, isolated_paths):
        _write_item(isolated_paths / "items", "a.md", "content")
        stale_hash = worker.compute_todo_state_hash(queue={"runs": []})
        _write_item(isolated_paths / "items", "a.md", "different content")

        _write_planner_state(
            isolated_paths,
            {"verdict": "exhausted", "todo_state_hash": stale_hash, "last_run": worker.now_iso()},
        )
        exhausted, reason = worker.is_planner_exhausted(queue={"runs": []})
        assert exhausted is False
        assert "hash changed" in reason

    def test_no_last_run_timestamp(self, isolated_paths):
        current_hash = worker.compute_todo_state_hash(queue={"runs": []})
        _write_planner_state(isolated_paths, {"verdict": "exhausted", "todo_state_hash": current_hash})
        exhausted, reason = worker.is_planner_exhausted(queue={"runs": []})
        assert exhausted is False
        assert reason == "no last_run timestamp"

    def test_invalid_last_run_timestamp(self, isolated_paths):
        current_hash = worker.compute_todo_state_hash(queue={"runs": []})
        _write_planner_state(
            isolated_paths,
            {"verdict": "exhausted", "todo_state_hash": current_hash, "last_run": "not-a-date"},
        )
        exhausted, reason = worker.is_planner_exhausted(queue={"runs": []})
        assert exhausted is False
        assert reason == "invalid last_run timestamp"

    def test_true_within_threshold(self, isolated_paths):
        current_hash = worker.compute_todo_state_hash(queue={"runs": []})
        last_run = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat()
        _write_planner_state(
            isolated_paths,
            {"verdict": "exhausted", "todo_state_hash": current_hash, "last_run": last_run},
        )
        exhausted, reason = worker.is_planner_exhausted(queue={"runs": []})
        assert exhausted is True
        assert "exhausted" in reason

    def test_just_under_threshold(self, isolated_paths):
        current_hash = worker.compute_todo_state_hash(queue={"runs": []})
        margin_hours = worker.PLANNER_EXHAUSTED_THRESHOLD_HOURS - 0.01
        last_run = (dt.datetime.now() - dt.timedelta(hours=margin_hours)).isoformat()
        _write_planner_state(
            isolated_paths,
            {"verdict": "exhausted", "todo_state_hash": current_hash, "last_run": last_run},
        )
        exhausted, reason = worker.is_planner_exhausted(queue={"runs": []})
        assert exhausted is True

    def test_at_or_over_threshold(self, isolated_paths):
        current_hash = worker.compute_todo_state_hash(queue={"runs": []})
        last_run = (
            dt.datetime.now() - dt.timedelta(hours=worker.PLANNER_EXHAUSTED_THRESHOLD_HOURS)
        ).isoformat()
        _write_planner_state(
            isolated_paths,
            {"verdict": "exhausted", "todo_state_hash": current_hash, "last_run": last_run},
        )
        exhausted, reason = worker.is_planner_exhausted(queue={"runs": []})
        assert exhausted is False
        assert "elapsed" in reason


# ---------------------------------------------------------------------------
# count_auto_pending (AC8)
# ---------------------------------------------------------------------------


class TestCountAutoPending:
    def test_filters_by_mode_and_status(self, isolated_paths):
        queue = {
            "runs": [
                {"status": "pending", "mode": "auto"},
                {"status": "pending", "mode": "delegate"},
                {"status": "pending", "mode": "quick"},
                {"status": "done", "mode": "auto"},
                {"status": "done", "mode": "delegate"},
            ]
        }
        assert worker.count_auto_pending(queue=queue) == 2

    def test_empty_runs_list(self, isolated_paths):
        assert worker.count_auto_pending(queue={"runs": []}) == 0

    def test_no_runs_key(self, isolated_paths):
        assert worker.count_auto_pending(queue={}) == 0

    def test_queue_none_uses_load_queue(self, isolated_paths):
        queue = {"runs": [{"status": "pending", "mode": "auto"}]}
        worker.save_queue(queue)
        assert worker.count_auto_pending() == worker.count_auto_pending(queue=queue)

    def test_missing_mode_or_status_keys_excluded(self, isolated_paths):
        queue = {
            "runs": [
                {"status": "pending"},
                {"mode": "auto"},
                {},
            ]
        }
        assert worker.count_auto_pending(queue=queue) == 0


# ---------------------------------------------------------------------------
# parse_frontmatter (AC9, AC10, AC11)
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_no_leading_delimiter_returns_empty(self):
        content = "not frontmatter\njust body text\n"
        result, body = worker.parse_frontmatter(content)
        assert result == {}
        assert body == content

    def test_no_closing_delimiter_returns_empty(self):
        content = "---\nstatus: open\nno closing delimiter here\n"
        result, body = worker.parse_frontmatter(content)
        assert result == {}
        assert body == content

    def test_normal_document_scalars_and_body(self):
        content = "---\nstatus: open\ndue: 2026-09-28\n---\n# Title\nbody text\n"
        result, body = worker.parse_frontmatter(content)
        assert result["status"] == "open"
        assert result["due"] == "2026-09-28"
        assert body == "# Title\nbody text\n"

    def test_strips_outer_quotes(self):
        content = '---\noutcome: "완료 조건"\nnext_action: \'do the thing\'\n---\nbody\n'
        result, _ = worker.parse_frontmatter(content)
        assert result["outcome"] == "완료 조건"
        assert result["next_action"] == "do the thing"

    def test_skips_lines_without_colon_and_blank_lines(self):
        content = "---\nstatus: open\n\nstray line with no colon\ndue: 2026-09-28\n---\nbody\n"
        result, _ = worker.parse_frontmatter(content)
        assert result == {"status": "open", "due": "2026-09-28"}

    def test_list_collects_dash_items_after_empty_value(self):
        content = "---\ntags:\n  - todo\n  - work\nstatus: open\n---\nbody\n"
        result, _ = worker.parse_frontmatter(content)
        assert result["tags"] == ["todo", "work"]
        assert result["status"] == "open"

    def test_list_breaks_on_non_dash_line(self):
        content = "---\ntags:\n  - todo\nstatus: open\n---\nbody\n"
        result, _ = worker.parse_frontmatter(content)
        assert result["tags"] == ["todo"]
        assert result["status"] == "open"

    def test_list_zero_items_is_empty_list(self):
        content = "---\ntags:\nstatus: open\n---\nbody\n"
        result, _ = worker.parse_frontmatter(content)
        assert result["tags"] == []
        assert result["status"] == "open"

    def test_inline_comment_not_stripped(self):
        content = "---\nstatus: open # open | done\n---\nbody\n"
        result, _ = worker.parse_frontmatter(content)
        assert result["status"] == "open # open | done"

    def test_two_space_indented_dash_items_still_parse(self):
        """Pins that `  - ` indented list items keep parsing after the
        unreachable `if sub.startswith("  - "):` branch is removed — the
        earlier `sub.strip().startswith("- ")` branch already matches
        indented items, so the second branch never ran."""
        content = "---\ntags:\n  - todo\n  - work\n  - macp\nstatus: open\n---\nbody\n"
        result, _ = worker.parse_frontmatter(content)
        assert result["tags"] == ["todo", "work", "macp"]
        assert result["status"] == "open"


# ---------------------------------------------------------------------------
# load_queue / save_queue / queue_lock (AC12, AC13)
# ---------------------------------------------------------------------------


class TestQueueIO:
    def test_load_queue_missing_file_returns_empty_runs(self, isolated_paths):
        assert worker.load_queue() == {"runs": []}

    def test_load_queue_corrupt_json_returns_empty_runs(self, isolated_paths):
        path = isolated_paths / "queue.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        assert worker.load_queue() == {"runs": []}

    def test_save_then_load_queue_roundtrip(self, isolated_paths):
        queue = {"runs": [{"run_id": "1", "status": "pending"}]}
        worker.save_queue(queue)
        assert worker.load_queue() == queue

    def test_save_queue_creates_parent_directories(self, isolated_paths):
        nested = isolated_paths / "nested" / "deep" / "queue.json"
        with mock.patch.object(worker, "QUEUE_PATH", nested):
            worker.save_queue({"runs": []})
        assert nested.exists()

    def test_save_queue_writes_non_ascii_verbatim(self, isolated_paths):
        worker.save_queue({"runs": [{"note": "한글"}]})
        raw = (isolated_paths / "queue.json").read_text(encoding="utf-8")
        assert "한글" in raw
        assert "\\u" not in raw

    def test_queue_lock_creates_parent_directory(self, isolated_paths):
        nested_lock = isolated_paths / "nested" / "deep" / "queue.lock"
        with mock.patch.object(worker, "QUEUE_LOCK_PATH", nested_lock):
            with worker.queue_lock():
                pass
        assert nested_lock.exists()

    def test_queue_lock_released_after_exception_allows_reentry(self, isolated_paths):
        with pytest.raises(ValueError):
            with worker.queue_lock():
                raise ValueError("boom")

        # If the lock weren't released, this would hang; it must not.
        with worker.queue_lock():
            pass
