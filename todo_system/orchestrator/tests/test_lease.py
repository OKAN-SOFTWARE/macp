"""Checkout-lease acquisition must be ATOMIC, and an unreadable lock must never
read as "free".

Two defects pinned here, both in `lease.Lease.acquire`:

  * **TOCTOU** — acquisition was `if self._held_by_other(now): return False` then
    `self._write(now)`. Two processes that both observe "free" both write, so two
    holders believe they own the one lock that serializes the live vault's
    HEAD-moving branch prep against the planner tick.
  * **torn lock read as free** — `_read()` collapsed "no file" and "cannot parse
    this file" into the same `None`, which `_held_by_other` turned into
    "free, take it". A lock file caught mid-write (or truncated by a crash) thus
    HANDED OUT the lock it was supposed to be holding.

The fix makes `O_CREAT|O_EXCL` create the primary acquisition path (no read, so
no check-then-write window), keeps renew/TTL-takeover/dead-PID-reclaim, and
treats an unparseable lock as held — with an mtime backstop so a 0-byte lock
cannot wedge the planner tick forever.

Every lock file here lives in a per-test tmpdir: touching the live
`Worker/checkout.lock` would really break live checkout serialization.
Complementary suites: `todo_system/tests/test_checkout_lease.py` (dead-PID
reclaim, CLI wiring, runner guard) and `test_orchestrator.py::LeaseTest`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import lease as lease_mod
from todo_system.orchestrator.lease import Lease

HOST = "test-host"


class _LeaseTmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "x.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _raw(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def _holder_on_disk(self) -> str:
        return json.loads(self._raw())["holder"]

    def _write_raw(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def _write_lock(self, holder: str, expires_at: float) -> None:
        self._write_raw(json.dumps({"holder": holder, "expires_at": expires_at}))

    def _siblings(self) -> list[str]:
        return sorted(p.name for p in self.tmp.glob("x.lock*"))


# ---------------------------------------------------------------------------
# RED #1 — acquisition must not depend on a read that can be stale
# ---------------------------------------------------------------------------
class AcquireIsAtomicTest(_LeaseTmpCase):
    def test_stale_free_observation_cannot_steal_a_held_lock(self):
        """The TOCTOU made executable: B is handed the exact observation the old
        code raced on — "the lock is free" — while A really holds it. Under
        check-then-write B overwrote A's lock and returned True. Under an
        atomic create the observation is irrelevant, because the create itself
        (not the read) decides."""
        a = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        b = Lease(self.path, holder="other-host:2", ttl=100)
        self.assertTrue(a.acquire(now=1000))

        with mock.patch.object(Lease, "_read", return_value=None):
            self.assertFalse(b.acquire(now=1000),
                             "a stale 'free' read let a second holder take the lock")

        self.assertEqual(self._holder_on_disk(), f"{HOST}:1",
                         "the held lock was overwritten by the loser")

    def test_parallel_acquire_has_exactly_one_winner(self):
        """No mocks: 8 threads released together onto one empty lock. Distinct
        hosts so nobody qualifies for the same-host dead-PID reclaim — the only
        way to win is to create the file."""
        n = 8
        barrier = threading.Barrier(n)
        results: list[bool] = []
        guard = threading.Lock()

        def contend(i: int) -> None:
            lz = Lease(self.path, holder=f"host{i}:{1000 + i}", ttl=100)
            barrier.wait()
            got = lz.acquire(now=1000)
            with guard:
                results.append(got)

        threads = [threading.Thread(target=contend, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(results), n, "a contending thread never finished")
        self.assertEqual(sum(results), 1,
                         f"expected exactly one winner, got {sum(results)}")

    def test_winner_of_a_race_is_the_holder_written_on_disk(self):
        """Exactly-one-winner is only useful if the winner is the holder the
        file names — otherwise the loser's write landed last."""
        a = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        b = Lease(self.path, holder="other-host:2", ttl=100)
        self.assertTrue(a.acquire(now=1000))
        self.assertFalse(b.acquire(now=1000))
        self.assertEqual(self._holder_on_disk(), f"{HOST}:1")


# ---------------------------------------------------------------------------
# RED #2 — an unreadable lock is HELD, never free
# ---------------------------------------------------------------------------
class UnreadableLockIsHeldTest(_LeaseTmpCase):
    UNREADABLE = {
        "truncated_json": '{"holder": "h:1", "expires_at": ',
        "empty_file": "",
        "no_keys": "{}",
        "json_array": '["a"]',
        "null_expiry": '{"holder": "h:1", "expires_at": null}',
        "expiry_not_a_number": '{"holder": "h:1", "expires_at": "soon"}',
    }

    def test_unreadable_lock_is_not_acquired(self):
        for name, payload in self.UNREADABLE.items():
            with self.subTest(shape=name):
                self._write_raw(payload)
                lz = Lease(self.path, holder=f"{HOST}:{os.getpid()}", ttl=100)
                self.assertFalse(lz.acquire(),
                                 f"unreadable lock ({name}) was read as free")
                self.assertEqual(self._raw(), payload,
                                 f"unreadable lock ({name}) was overwritten")

    def test_unreadable_lock_reports_rc1_not_a_crash(self):
        """`planner_tick.sh` treats rc 0 as taken, rc 1 as "someone else holds
        it" and ANY OTHER rc as a CLI error it fails OPEN on. A shape that makes
        the CLI raise therefore removes the protection entirely."""
        for name, payload in self.UNREADABLE.items():
            with self.subTest(shape=name):
                self._write_raw(payload)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = lease_mod._main(["acquire", "--path", str(self.path),
                                          "--holder", "planner:1"])
                self.assertEqual(rc, 1,
                                 f"unreadable lock ({name}) did not map to rc 1; "
                                 "planner_tick would fail open")

    def test_unreadable_lock_older_than_the_ttl_is_reclaimed(self):
        """The backstop: a crash between create and write leaves a 0-byte lock
        whose TTL cannot be read, so without an mtime fallback the planner tick
        would skip forever. mtime does for an unreadable lock what expires_at
        does for a readable one."""
        self._write_raw("")
        old = time.time() - 5000
        os.utime(self.path, (old, old))

        lz = Lease(self.path, holder=f"{HOST}:{os.getpid()}", ttl=100)
        self.assertTrue(lz.acquire(), "a long-stale unreadable lock wedged the lease")
        self.assertEqual(self._holder_on_disk(), f"{HOST}:{os.getpid()}")
        self.assertEqual(self._siblings(), ["x.lock"], "takeover left debris behind")

    def test_fresh_unreadable_lock_is_not_reclaimed_by_the_backstop(self):
        """The mtime backstop must not become a second "torn lock is free" path:
        a lock written a moment ago is held even though it cannot be parsed."""
        self._write_raw("")
        lz = Lease(self.path, holder=f"{HOST}:{os.getpid()}", ttl=100)
        self.assertFalse(lz.acquire())
        self.assertEqual(self._raw(), "")


# ---------------------------------------------------------------------------
# Preserved contracts — these are what the atomic rewrite must not cost us
# ---------------------------------------------------------------------------
class PreservedContractsTest(_LeaseTmpCase):
    def test_renew_by_the_same_holder_succeeds_and_extends(self):
        """A pure-O_EXCL acquire would fail here forever, and the orchestrator
        renews its lease every tick."""
        me = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        self.assertTrue(me.acquire(now=1000))
        self.assertEqual(json.loads(self._raw())["expires_at"], 1100)
        self.assertTrue(me.renew(now=1500))
        self.assertEqual(json.loads(self._raw())["expires_at"], 1600)
        self.assertEqual(self._holder_on_disk(), f"{HOST}:1")
        self.assertEqual(self._siblings(), ["x.lock"], "renew left a tmp file behind")

    def test_expired_lock_is_taken_over(self):
        self._write_lock("other-host:9", expires_at=900)
        me = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        self.assertTrue(me.acquire(now=1000))
        self.assertEqual(self._holder_on_disk(), f"{HOST}:1")
        self.assertEqual(self._siblings(), ["x.lock"], "takeover left debris behind")

    def test_same_host_dead_pid_is_reclaimed_before_the_ttl(self):
        self._write_lock(f"{HOST}:424242", expires_at=9e9)
        me = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        with mock.patch.object(lease_mod, "_pid_alive", return_value=False):
            self.assertTrue(me.acquire(now=1000))
        self.assertEqual(self._holder_on_disk(), f"{HOST}:1")

    def test_remote_host_holder_is_never_reclaimed(self):
        self._write_lock("some-other-host:424242", expires_at=9e9)
        me = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        with mock.patch.object(lease_mod, "_pid_alive", return_value=False):
            self.assertFalse(me.acquire(now=1000))

    def test_unparseable_holder_string_is_never_reclaimed(self):
        """The holder is unparseable but the lock itself parses — expires_at is
        readable, so the TTL (not the unknown path) is the only way out."""
        self._write_lock("not-a-host-pid", expires_at=9e9)
        me = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        with mock.patch.object(lease_mod, "_pid_alive", return_value=False):
            self.assertFalse(me.acquire(now=1000))

    def test_live_same_host_pid_is_held(self):
        self._write_lock(f"{HOST}:424242", expires_at=9e9)
        me = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        with mock.patch.object(lease_mod, "_pid_alive", return_value=True):
            self.assertFalse(me.acquire(now=1000))

    def test_release_only_unlinks_my_own_lock(self):
        a = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        b = Lease(self.path, holder="other-host:2", ttl=100)
        self.assertTrue(a.acquire(now=1000))
        b.release()
        self.assertTrue(self.path.exists(), "release dropped another holder's lock")
        a.release()
        self.assertFalse(self.path.exists())

    def test_release_leaves_an_unreadable_lock_alone(self):
        self._write_raw("{")
        Lease(self.path, holder=f"{HOST}:1", ttl=100).release()
        self.assertTrue(self.path.exists(),
                        "release unlinked a lock it could not prove was ours")

    def test_current_reports_the_holder_and_none_when_free(self):
        lz = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        self.assertIsNone(lz.current())
        self.assertTrue(lz.acquire(now=1000))
        cur = lz.current()
        self.assertIsNotNone(cur)
        self.assertEqual(cur.holder, f"{HOST}:1")
        self.assertEqual(cur.expires_at, 1100)

    def test_acquire_accepts_an_injected_now(self):
        """Signature contract: `now` stays injectable, and time.time() is not
        consulted when it is supplied."""
        lz = Lease(self.path, holder=f"{HOST}:1", ttl=100)
        with mock.patch.object(lease_mod.time, "time",
                               side_effect=AssertionError("time.time() was read")):
            self.assertTrue(lz.acquire(now=1000))
        self.assertEqual(json.loads(self._raw())["expires_at"], 1100)


class CliReturnCodeTest(_LeaseTmpCase):
    """`planner_tick.sh` fails OPEN on any rc other than 0/1, so the rc mapping
    is part of the protection, not cosmetics."""

    def _run(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lease_mod._main(list(argv))
        return rc, buf.getvalue()

    def test_rc_mapping_over_a_full_cycle(self):
        rc, _ = self._run("acquire", "--path", str(self.path), "--holder", "planner:1")
        self.assertEqual(rc, 0)

        rc, out = self._run("acquire", "--path", str(self.path), "--holder", "other:2")
        self.assertEqual(rc, 1)
        self.assertIn("planner:1", out)

        rc, _ = self._run("renew", "--path", str(self.path), "--holder", "planner:1")
        self.assertEqual(rc, 0)

        rc, out = self._run("current", "--path", str(self.path))
        self.assertEqual(rc, 0)
        self.assertIn("planner:1", out)

        rc, _ = self._run("release", "--path", str(self.path), "--holder", "planner:1")
        self.assertEqual(rc, 0)
        self.assertFalse(self.path.exists())

        rc, out = self._run("current", "--path", str(self.path))
        self.assertEqual(rc, 0)
        self.assertIn("free", out)

    def test_acquire_leaves_no_temp_or_stale_debris(self):
        rc, _ = self._run("acquire", "--path", str(self.path), "--holder", "planner:1")
        self.assertEqual(rc, 0)
        rc, _ = self._run("renew", "--path", str(self.path), "--holder", "planner:1")
        self.assertEqual(rc, 0)
        self.assertEqual(self._siblings(), ["x.lock"])


if __name__ == "__main__":
    unittest.main()
