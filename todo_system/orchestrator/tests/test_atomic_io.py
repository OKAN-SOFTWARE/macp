"""Tests for the shared atomic_write_text/atomic_write_json helper."""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import atomic_io


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_replace_crash_preserves_original_and_leaves_no_tmp(self):
        path = self.tmp / "state.json"
        path.write_text('{"seen": ["a"]}', encoding="utf-8")

        with mock.patch.object(atomic_io.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_io.atomic_write_json(path, {"seen": ["a", "b"]})

        self.assertEqual(path.read_text(encoding="utf-8"), '{"seen": ["a"]}')
        leftovers = list(self.tmp.glob("state.json.tmp.*"))
        self.assertEqual(leftovers, [])

    def test_tmp_write_crash_never_calls_replace_and_preserves_original(self):
        path = self.tmp / "state.json"
        path.write_text('{"seen": ["a"]}', encoding="utf-8")

        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with mock.patch.object(atomic_io.os, "replace") as replace_mock:
                with self.assertRaises(OSError):
                    atomic_io.atomic_write_json(path, {"seen": ["a", "b"]})
                replace_mock.assert_not_called()

        self.assertEqual(path.read_text(encoding="utf-8"), '{"seen": ["a"]}')

    def test_successful_write_replaces_content_and_cleans_tmp(self):
        path = self.tmp / "state.json"
        atomic_io.atomic_write_json(path, {"seen": ["a"]})
        self.assertIn('"a"', path.read_text(encoding="utf-8"))
        leftovers = list(self.tmp.glob("state.json.tmp.*"))
        self.assertEqual(leftovers, [])

    def test_same_process_concurrent_writers_do_not_collide_on_tmp_name(self):
        path = self.tmp / "state.json"
        path.write_text('{"seen": []}', encoding="utf-8")

        texts = [f'{{"seen": ["{i}"]}}' for i in range(8)]
        errors = []

        def writer(text):
            try:
                atomic_io.atomic_write_text(path, text)
            except Exception as exc:  # pragma: no cover - failure path only
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in texts]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [])
        self.assertIn(path.read_text(encoding="utf-8"), texts)
        leftovers = list(self.tmp.glob("state.json.tmp.*"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
