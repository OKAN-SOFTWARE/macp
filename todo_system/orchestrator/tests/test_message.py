"""Contract tests for the Envelope dataclass and its module-level helpers."""

from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from todo_system.orchestrator import message as m


class EnvelopeValidationTest(unittest.TestCase):
    def test_unknown_type_raises(self):
        with self.assertRaisesRegex(ValueError, "BOGUS"):
            m.Envelope(to="a", type="BOGUS")

    def test_empty_to_raises(self):
        with self.assertRaises(ValueError):
            m.Envelope(to="", type=m.WAKE)


class EnvelopeDefaultsTest(unittest.TestCase):
    def test_idempotency_key_defaults_to_msg_id(self):
        e = m.Envelope(to="a", type=m.WAKE)
        self.assertEqual(e.idempotency_key, e.msg_id)

    def test_idempotency_key_explicit_kept(self):
        e = m.Envelope(to="a", type=m.WAKE, idempotency_key="ik1")
        self.assertEqual(e.idempotency_key, "ik1")

    def test_corr_id_defaults_to_msg_id(self):
        e = m.Envelope(to="a", type=m.WAKE)
        self.assertEqual(e.corr_id, e.msg_id)

    def test_corr_id_explicit_kept(self):
        e = m.Envelope(to="a", type=m.WAKE, corr_id="c1")
        self.assertEqual(e.corr_id, "c1")


class EnvelopeReplyTest(unittest.TestCase):
    def test_reply_reverses_addresses_and_preserves_conversation(self):
        assign = m.Envelope(to="agentX", type=m.TASK_ASSIGN, task_id="t1")
        rep = assign.reply(m.ACCEPT)
        self.assertEqual(rep.to, assign.frm)
        self.assertEqual(rep.frm, assign.to)
        self.assertEqual(rep.reply_to, assign.to)
        self.assertEqual(rep.task_id, assign.task_id)
        self.assertEqual(rep.corr_id, assign.corr_id)
        self.assertNotEqual(rep.msg_id, assign.msg_id)

    def test_reply_to_takes_priority_over_frm(self):
        assign = m.Envelope(to="agentX", type=m.TASK_ASSIGN, reply_to="mediator")
        rep = assign.reply(m.ACCEPT)
        self.assertEqual(rep.to, "mediator")
        self.assertNotEqual(rep.to, assign.frm)

    def test_reply_chain_preserves_corr_id_across_three_hops(self):
        dispatch = m.Envelope(to="agentX", type=m.TASK_ASSIGN, task_id="t1")
        reply1 = dispatch.reply(m.ACK)
        reply2 = reply1.reply(m.PROGRESS)
        reply3 = reply2.reply(m.RESULT)
        corr_ids = {dispatch.corr_id, reply1.corr_id, reply2.corr_id, reply3.corr_id}
        self.assertEqual(len(corr_ids), 1)
        msg_ids = {dispatch.msg_id, reply1.msg_id, reply2.msg_id, reply3.msg_id}
        self.assertEqual(len(msg_ids), 4)


class EnvelopeDictJsonTest(unittest.TestCase):
    def test_to_dict_has_from_key_not_frm(self):
        d = m.Envelope(to="a", type=m.WAKE).to_dict()
        self.assertIn("from", d)
        self.assertNotIn("frm", d)
        self.assertEqual(d["from"], m.ORCHESTRATOR)

    def test_to_dict_key_set_exact(self):
        d = m.Envelope(to="a", type=m.WAKE).to_dict()
        self.assertEqual(
            set(d.keys()),
            {
                "msg_id",
                "task_id",
                "from",
                "to",
                "type",
                "reply_to",
                "corr_id",
                "ts",
                "idempotency_key",
                "payload",
            },
        )

    def test_from_dict_missing_from_defaults_orchestrator(self):
        d = {"to": "a", "type": m.WAKE}
        e = m.Envelope.from_dict(d)
        self.assertEqual(e.frm, m.ORCHESTRATOR)

    def test_from_dict_missing_or_none_payload_defaults_empty(self):
        e_missing = m.Envelope.from_dict({"to": "a", "type": m.WAKE})
        self.assertEqual(e_missing.payload, {})
        e_none = m.Envelope.from_dict({"to": "a", "type": m.WAKE, "payload": None})
        self.assertEqual(e_none.payload, {})

    def test_from_dict_missing_msg_id_generates_new_26char_id(self):
        e = m.Envelope.from_dict({"to": "a", "type": m.WAKE})
        self.assertEqual(len(e.msg_id), 26)
        self.assertTrue(all(c in m._CROCKFORD for c in e.msg_id))

    def test_from_dict_missing_ts_generates_now_iso_format(self):
        e = m.Envelope.from_dict({"to": "a", "type": m.WAKE})
        parsed = dt.datetime.fromisoformat(e.ts)
        self.assertIsNotNone(parsed.tzinfo)

    def test_roundtrip_via_json(self):
        e = m.Envelope(to="a", type=m.TASK_ASSIGN, task_id="t1")
        back = m.Envelope.from_json(e.to_json())
        self.assertEqual(back.to_dict(), e.to_dict())


class EncodeTest(unittest.TestCase):
    def test_encode_zero_pads_length(self):
        self.assertEqual(m._encode(0, 4), "0000")

    def test_encode_max_single_char(self):
        self.assertEqual(m._encode(31, 1), "Z")

    def test_encode_alphabet_excludes_ambiguous_letters(self):
        for ch in "ILOU":
            self.assertNotIn(ch, m._CROCKFORD)

    def test_encode_rejects_value_wider_than_length(self):
        # Silently dropping high bits would turn 32 into "0" and a wrapped
        # 80-bit counter into "0"*16, breaking the sort order of ids.
        with self.assertRaises(ValueError):
            m._encode(32, 1)
        with self.assertRaises(ValueError):
            m._encode(1 << 80, 16)

    def test_encode_accepts_max_value_for_length(self):
        self.assertEqual(m._encode((1 << 80) - 1, 16), "Z" * 16)
        self.assertEqual(m._encode((1 << 50) - 1, 10), "Z" * 10)

    def test_encode_rejects_negative(self):
        with self.assertRaises(ValueError):
            m._encode(-1, 4)


class NewIdMonotonicTest(unittest.TestCase):
    def setUp(self):
        self._saved_ms = m._last_ms
        self._saved_rand = m._last_rand

    def tearDown(self):
        m._last_ms = self._saved_ms
        m._last_rand = self._saved_rand

    def test_same_ms_ids_strictly_increasing_and_length_26(self):
        with mock.patch.object(m.time, "time", return_value=1_700_000_000.123):
            m._last_ms = 0
            m._last_rand = 0
            ids = [m.new_id() for _ in range(3)]
        self.assertLess(ids[0], ids[1])
        self.assertLess(ids[1], ids[2])
        self.assertTrue(all(len(i) == 26 for i in ids))

    T = 1_700_000_000.123
    MS = int(T * 1000)

    def test_same_ms_rand_overflow_stays_monotonic(self):
        m._last_ms = self.MS
        m._last_rand = 2**80 - 1
        prev = m._encode(self.MS, 10) + m._encode(2**80 - 1, 16)
        with mock.patch.object(m.time, "time", return_value=self.T):
            a = m.new_id()
            b = m.new_id()
        self.assertLess(prev, a)
        self.assertLess(a, b)
        self.assertEqual(len(a), 26)
        self.assertEqual(len(b), 26)
        self.assertEqual(a[:10], m._encode(self.MS + 1, 10))
        self.assertEqual(m._last_ms, self.MS + 1)
        self.assertLess(m._last_rand, 2**80)

    def test_overflow_reseed_is_deterministic_under_patched_urandom(self):
        m._last_ms = self.MS
        m._last_rand = 2**80 - 1
        with (
            mock.patch.object(m.time, "time", return_value=self.T),
            mock.patch.object(m.os, "urandom", return_value=b"\x00" * 10),
        ):
            a = m.new_id()
        self.assertEqual(a, m._encode(self.MS + 1, 10) + "0" * 16)

    def test_clock_step_back_does_not_regress_id(self):
        m._last_ms = self.MS + 5
        m._last_rand = 123
        prev = m._encode(self.MS + 5, 10) + m._encode(123, 16)
        with mock.patch.object(m.time, "time", return_value=self.T):
            a = m.new_id()
        self.assertLess(prev, a)
        self.assertEqual(a[:10], m._encode(self.MS + 5, 10))


class NowIsoTest(unittest.TestCase):
    def test_now_iso_reparses_and_is_tz_aware_with_zero_microsecond(self):
        s = m.now_iso()
        parsed = dt.datetime.fromisoformat(s)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.microsecond, 0)


if __name__ == "__main__":
    unittest.main()
