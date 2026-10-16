"""Tests for the ``db_connect(require_schema=True)`` guard in ``todo_system.cli``.

An uninitialized DB file (exists, no ``tasks`` table — e.g. a freshly cloned
vault, or a typo'd ``MACP_VAULT_ROOT``) used to make the pure-read commands
(``list``, ``audit``, ...) die with a raw ``sqlite3.OperationalError``
traceback. These tests drive the real ``python -m todo_system.cli`` entry
point (matching the acceptance criteria's literal repro command) against a
``tmp_path`` DB file — never ``Todo/todo.db``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "todo_system.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )


def test_list_on_empty_db_prints_guidance_and_nonzero_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    db_path.touch()

    result = _run_cli("--db", str(db_path), "list")

    assert result.returncode != 0
    assert "no such table" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "init-db" in result.stderr


def test_audit_on_empty_db_prints_guidance_and_nonzero_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    db_path.touch()

    result = _run_cli("--db", str(db_path), "audit")

    assert result.returncode != 0
    assert "no such table" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "init-db" in result.stderr


def test_init_db_still_self_initializes(tmp_path: Path) -> None:
    db_path = tmp_path / "new.db"

    init_result = _run_cli("--db", str(db_path), "init-db")
    assert init_result.returncode == 0

    list_result = _run_cli("--db", str(db_path), "list")
    assert list_result.returncode == 0
