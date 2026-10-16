"""Tests for the crash-durable claim store (claims.py)."""

from __future__ import annotations

import shutil
import socket
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import claims
from todo_system.orchestrator import message as m


def _assign(**kw) -> m.Envelope:
    base = dict(to="armchair-commander", type=m.TASK_ASSIGN, task_id="core-loop",
                payload={"task_path": "Todo/items/core-loop.md"})
    base.update(kw)
    return m.Envelope(**base)


class ClaimStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = claims.ClaimStore(self.tmp / "agents", "armchair-commander")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_claim_lands_under_gitignored_state_not_the_inbox(self):
        msg = _assign()
        self.store.open(msg, pid=4242, host="mac")
        p = self.store.path(msg.msg_id)
        self.assertTrue(p.exists())
        # _state/<agent>/claims/ is the local-runtime tree (.gitignore'd); the
        # transport's inbox/ is git-synced and must never carry execution state.
        self.assertEqual(p.parent.parent.parent.name, "_state")
        self.assertNotIn("inbox", p.parts)

    def test_open_read_roundtrip_identifies_the_run(self):
        msg = _assign()
        self.store.open(msg, pid=4242, host="mac")
        got = self.store.read(msg.msg_id)
        # the fields the report stage needs instead of timestamp archaeology
        self.assertEqual(got["msg_id"], msg.msg_id)
        self.assertEqual(got["task_id"], "core-loop")
        self.assertEqual(got["task_path"], "Todo/items/core-loop.md")
        self.assertEqual(got["corr_id"], msg.corr_id)
        self.assertEqual(got["pid"], 4242)
        self.assertEqual(got["host"], "mac")
        self.assertTrue(got["started_at"])
        self.assertIsNone(got["run_id"])

    def test_open_records_a_payload_branch_override(self):
        msg = _assign(payload={"branch": "custom/experiment"})
        self.store.open(msg, pid=1)
        self.assertEqual(self.store.read(msg.msg_id)["branch"], "custom/experiment")

    def test_open_leaves_branch_none_when_the_payload_does_not_override(self):
        # Not `worker/core-loop`: recomputing the default here would fork the
        # formula away from runner.branch_for. None means "ask branch_for".
        msg = _assign()
        self.store.open(msg, pid=1)
        self.assertIsNone(self.store.read(msg.msg_id)["branch"])

    def test_attach_run_binds_the_run_dir(self):
        msg = _assign()
        self.store.open(msg, pid=1)
        self.store.attach_run(msg.msg_id, "20260818T010203-core-loop", "/vault/Worker/runs/x")
        got = self.store.read(msg.msg_id)
        self.assertEqual(got["run_id"], "20260818T010203-core-loop")
        self.assertEqual(got["run_dir"], "/vault/Worker/runs/x")

    def test_attach_run_without_a_claim_is_a_noop(self):
        self.store.attach_run("01NOPE", "r", "/d")   # must not raise or create one
        self.assertIsNone(self.store.read("01NOPE"))

    def test_missing_and_corrupt_claims_read_as_absent(self):
        self.assertIsNone(self.store.read("01MISSING"))
        self.store.dir.mkdir(parents=True, exist_ok=True)
        (self.store.dir / "01BROKEN.json").write_text("{", encoding="utf-8")
        # absent, not an exception: the envelope keeps being redelivered (today's
        # behaviour) rather than being silently swallowed.
        self.assertIsNone(self.store.read("01BROKEN"))

    def test_note_redelivery_appends_history(self):
        msg = _assign()
        self.store.open(msg, pid=1)
        self.store.note_redelivery(msg.msg_id, claims.ORPHAN, "pid gone")
        self.store.note_redelivery(msg.msg_id, claims.ORPHAN, "pid still gone")
        hist = self.store.read(msg.msg_id)["redeliveries"]
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["verdict"], claims.ORPHAN)
        self.assertEqual(hist[1]["evidence"], "pid still gone")

    def test_close_removes_the_claim_and_is_idempotent(self):
        msg = _assign()
        self.store.open(msg, pid=1)
        self.store.close(msg.msg_id)
        self.assertIsNone(self.store.read(msg.msg_id))
        self.store.close(msg.msg_id)          # already gone — must not raise

    def test_all_lists_readable_claims(self):
        a, b = _assign(), _assign(task_id="other")
        self.store.open(a, pid=1)
        self.store.open(b, pid=2)
        self.assertEqual({c["msg_id"] for c in self.store.all()}, {a.msg_id, b.msg_id})


class ClassifyTest(unittest.TestCase):
    """The liveness ladder. `alive` is injected so no real pid is ever probed."""

    def _claim(self, **kw) -> dict:
        base = dict(msg_id="01X", task_id="t", pid=4242, host=socket.gethostname(),
                    started_at=m.now_iso(), run_dir="/vault/Worker/runs/r")
        base.update(kw)
        return base

    def test_dead_pid_is_an_orphan(self):
        v, why = claims.classify(self._claim(), alive=lambda _: False)
        self.assertEqual(v, claims.ORPHAN)
        self.assertIn("4242", why)

    def test_live_pid_is_live(self):
        v, why = claims.classify(self._claim(), alive=lambda _: True)
        self.assertEqual(v, claims.LIVE)
        self.assertIn("alive", why)

    def test_other_host_is_unknown_without_probing(self):
        probed = []

        def _alive(pid):
            probed.append(pid)
            return False

        v, why = claims.classify(self._claim(host="some-other-box"), alive=_alive)
        self.assertEqual(v, claims.UNKNOWN)
        self.assertEqual(probed, [])          # never probe another host's pid
        self.assertIn("some-other-box", why)

    def test_missing_pid_is_unknown(self):
        v, why = claims.classify(self._claim(pid=None), alive=lambda _: True)
        self.assertEqual(v, claims.UNKNOWN)
        self.assertIn("no pid", why)

    def test_live_pid_with_no_run_dir_and_an_old_claim_is_unknown(self):
        # pid reuse: the number answers, but nothing ties it to this run.
        v, why = claims.classify(
            self._claim(run_dir=None, started_at="2020-01-01T00:00:00+09:00"),
            alive=lambda _: True, stale_after=60.0)
        self.assertEqual(v, claims.UNKNOWN)
        self.assertIn("recycled", why)

    def test_live_pid_with_run_dir_stays_live_however_old(self):
        # stages write run.log only when they finish, so an hour of silence is
        # normal — staleness is never on its own evidence of death.
        v, _ = claims.classify(
            self._claim(started_at="2020-01-01T00:00:00+09:00"),
            alive=lambda _: True, stale_after=60.0)
        self.assertEqual(v, claims.LIVE)

    def test_unparseable_timestamp_never_raises(self):
        v, _ = claims.classify(self._claim(started_at="not-a-date"), alive=lambda _: False)
        self.assertEqual(v, claims.ORPHAN)


if __name__ == "__main__":
    unittest.main()
