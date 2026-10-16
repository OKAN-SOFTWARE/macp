"""not_before helper: the three accepted shapes, the 16:59/17:00 boundary, and
the "no gate" values every caller relies on."""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch

from todo_system import not_before as nb

D = dt.datetime


class ParseNotBefore(unittest.TestCase):
    def test_date_only_is_midnight(self):
        self.assertEqual(nb.parse_not_before("2026-10-12"), D(2026, 10, 12))

    def test_space_and_t_separators(self):
        for v in ("2026-10-12 17:00", "2026-10-12T17:00"):
            with self.subTest(v=v):
                self.assertEqual(nb.parse_not_before(v), D(2026, 10, 12, 17, 0))

    def test_seconds(self):
        self.assertEqual(nb.parse_not_before("2026-10-12T17:00:30"),
                         D(2026, 10, 12, 17, 0, 30))
        self.assertEqual(nb.parse_not_before("2026-10-12 17:00:30"),
                         D(2026, 10, 12, 17, 0, 30))

    def test_leftover_quotes_and_whitespace(self):
        self.assertEqual(nb.parse_not_before('"2026-10-12 17:00"'), D(2026, 10, 12, 17, 0))
        self.assertEqual(nb.parse_not_before("  '2026-10-12'  "), D(2026, 10, 12))

    def test_date_and_datetime_objects(self):
        self.assertEqual(nb.parse_not_before(dt.date(2026, 10, 12)), D(2026, 10, 12))
        self.assertEqual(nb.parse_not_before(D(2026, 10, 12, 17, 0)), D(2026, 10, 12, 17, 0))
        aware = D(2026, 10, 12, 8, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(nb.parse_not_before(aware),
                         aware.astimezone().replace(tzinfo=None))
        self.assertIsNone(nb.parse_not_before(aware).tzinfo)

    def test_no_gate_values(self):
        for v in (None, "", "-", "  ", '"-"', "soon"):
            with self.subTest(v=v):
                self.assertIsNone(nb.parse_not_before(v))

    def test_trailing_note_keeps_date_gate(self):
        # The old gates compared `nb[:10]`; a dated value with a note stays a date gate.
        self.assertEqual(nb.parse_not_before("2026-10-12 (after exam)"), D(2026, 10, 12))


class IsDeferred(unittest.TestCase):
    def test_time_boundary(self):
        for v in ("2026-10-12 17:00", "2026-10-12T17:00"):
            with self.subTest(v=v):
                self.assertTrue(nb.is_deferred(v, D(2026, 10, 12, 16, 59)))
                self.assertFalse(nb.is_deferred(v, D(2026, 10, 12, 17, 0)))
                self.assertFalse(nb.is_deferred(v, D(2026, 10, 12, 17, 1)))

    def test_date_only_matches_old_date_gt_today(self):
        self.assertTrue(nb.is_deferred("2026-10-13", D(2026, 10, 12, 23, 59)))
        self.assertFalse(nb.is_deferred("2026-10-13", D(2026, 10, 13, 0, 0)))
        self.assertFalse(nb.is_deferred("2026-10-12", D(2026, 10, 12, 0, 0)))

    def test_no_gate_never_deferred(self):
        for v in (None, "", "-", "soon"):
            with self.subTest(v=v):
                self.assertFalse(nb.is_deferred(v, D(1970, 1, 1)))

    def test_now_as_date_is_midnight(self):
        self.assertTrue(nb.is_deferred("2026-10-12 17:00", dt.date(2026, 10, 12)))
        self.assertFalse(nb.is_deferred("2026-10-12", dt.date(2026, 10, 12)))
        self.assertTrue(nb.is_deferred("2026-10-13", dt.date(2026, 10, 12)))

    def test_default_clock_is_patchable(self):
        with patch.object(nb, "_now", return_value=D(2026, 10, 12, 16, 59)):
            self.assertTrue(nb.is_deferred("2026-10-12 17:00"))
        with patch.object(nb, "_now", return_value=D(2026, 10, 12, 17, 0)):
            self.assertFalse(nb.is_deferred("2026-10-12 17:00"))


class DisplayNotBefore(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(nb.display_not_before("2026-10-12"), "2026-10-12")
        self.assertEqual(nb.display_not_before("2026-10-12 17:00"), "2026-10-12T17:00")
        self.assertEqual(nb.display_not_before("2026-10-12T17:00:30"), "2026-10-12T17:00")
        self.assertEqual(nb.display_not_before("2026-10-12 00:00"), "2026-10-12")
        for v in (None, "", "-", "soon"):
            with self.subTest(v=v):
                self.assertEqual(nb.display_not_before(v), "")


if __name__ == "__main__":
    unittest.main()
