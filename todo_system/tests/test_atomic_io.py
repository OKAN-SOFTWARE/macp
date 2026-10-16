"""Pin the torn-state-file guarantees of orchestrator.atomic_io.

These writers back the mailbox cursor and the assignment ledger, where a
half-written file has previously caused redelivery and silent-loss bugs.
The tests below exercise both the happy path and an injected mid-write
failure so the "old content survives, no temp residue" contract is actually
exercised, not just assumed.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest import mock

import pytest

from todo_system.orchestrator import atomic_io


def _tmp_residue(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if ".tmp." in p.name]


def test_write_leaves_no_tmp_residue(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_io.atomic_write_text(target, "hello world")

    assert target.read_text(encoding="utf-8") == "hello world"
    assert _tmp_residue(tmp_path) == []


def test_overwrite_with_shorter_content_leaves_no_trailing_tail(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_io.atomic_write_text(target, "a much longer original content that will be replaced")

    atomic_io.atomic_write_text(target, "short")

    assert target.read_text(encoding="utf-8") == "short"


def test_overwrite_with_shorter_content_leaves_no_tmp_residue(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_io.atomic_write_text(target, "a much longer original content that will be replaced")

    atomic_io.atomic_write_text(target, "short")

    assert _tmp_residue(tmp_path) == []


def test_non_default_encoding_round_trips_and_is_not_utf8(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    original = "한글 값"

    atomic_io.atomic_write_text(target, original, encoding="utf-16")

    assert target.read_text(encoding="utf-16") == original
    with pytest.raises(UnicodeDecodeError):
        target.read_text(encoding="utf-8")


def test_exception_during_write_preserves_original_and_cleans_tmp(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_io.atomic_write_text(target, "original content")

    with mock.patch.object(Path, "write_text", side_effect=OSError("injected")):
        with pytest.raises(OSError, match="injected"):
            atomic_io.atomic_write_text(target, "new content that must not land")

    assert target.read_text(encoding="utf-8") == "original content"
    assert _tmp_residue(tmp_path) == []


def test_exception_during_fsync_preserves_original_and_cleans_tmp(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_io.atomic_write_text(target, "original content")

    with mock.patch.object(os, "fsync", side_effect=OSError("injected fsync failure")):
        with pytest.raises(OSError, match="injected fsync failure"):
            atomic_io.atomic_write_text(target, "new content that must not land")

    assert target.read_text(encoding="utf-8") == "original content"
    assert _tmp_residue(tmp_path) == []


def test_parent_directory_auto_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "state.json"

    atomic_io.atomic_write_text(target, "payload")

    assert target.read_text(encoding="utf-8") == "payload"


def test_concurrent_writes_no_exception_and_final_content_is_one_of_two(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(payload: str) -> None:
        barrier.wait()
        try:
            atomic_io.atomic_write_text(target, payload)
        except BaseException as exc:  # noqa: BLE001 - captured for the test assertion below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert target.read_text(encoding="utf-8") in {"A", "B"}
    assert _tmp_residue(tmp_path) == []


def test_json_write_does_not_escape_korean(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    payload = {"제목": "한글 값"}

    atomic_io.atomic_write_json(target, payload)

    raw = target.read_text(encoding="utf-8")
    assert "\\u" not in raw
    assert "한글 값" in raw


def test_json_write_default_indent_is_two(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    payload = {"a": 1, "b": [1, 2, 3]}

    atomic_io.atomic_write_json(target, payload)

    assert target.read_text(encoding="utf-8") == json.dumps(payload, ensure_ascii=False, indent=2)


def test_json_write_custom_indent_respected(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    payload = {"a": 1, "b": [1, 2, 3]}

    atomic_io.atomic_write_json(target, payload, indent=4)

    assert target.read_text(encoding="utf-8") == json.dumps(payload, ensure_ascii=False, indent=4)
