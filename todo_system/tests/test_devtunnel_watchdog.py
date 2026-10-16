"""devtunnel_watchdog: parse, verdict table, incident sequences, and main's side effects."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import devtunnel_watchdog as W  # noqa: E402

# Shape of real `devtunnel show macp-dash` output (2026-09-25, values changed).
SHOW = """Tunnel ID             : demo-tunnel.use
Description           :
Access control        : {+Anonymous [connect]}
Host connections      : HOSTS
Client connections    : 0
Ports                 : 1
  8787  auto  https://example-8787.use.devtunnels.ms/  0 client connections
Tunnel Expiration     : 30 days
"""

UP = 3600.0


class Parse(unittest.TestCase):
    def test_host_connections(self):
        self.assertEqual(W.parse_host_connections(SHOW.replace("HOSTS", "0")), 0)
        self.assertEqual(W.parse_host_connections(SHOW.replace("HOSTS", "2")), 2)
        # "Client connections" and the per-port "client connections" must not match.
        self.assertIsNone(W.parse_host_connections("Client connections : 3\n"))
        self.assertIsNone(W.parse_host_connections(""))


class Verdict(unittest.TestCase):
    def v(self, hosts, state=None, active=True, uptime=UP, now=10_000.0):
        return W.verdict(hosts, active, uptime, state or {}, now)

    def test_table(self):
        self.assertEqual(self.v(1), "ok")
        self.assertEqual(self.v(0, active=False), "inactive")
        self.assertEqual(self.v(None, active=False), "inactive")
        self.assertEqual(self.v(0, uptime=30), "starting")
        self.assertEqual(self.v(0), "suspect")
        self.assertEqual(self.v(0, {"zero_since": 9_700.0}), "restart")
        self.assertEqual(self.v(0, {"zero_since": 9_700.0, "restarted_at": 9_700.0}), "stuck")
        self.assertEqual(self.v(0, {"zero_since": 1.0, "restarted_at": 1.0}), "restart")
        self.assertEqual(self.v(None), "unknown")
        self.assertEqual(self.v(None, {"query_fail_since": 9_700.0}), "query_failed")


def run_ticks(hosts_seq, start=0.0, step=300.0):
    """Drive verdict/next_state through a tick sequence; return the verdicts."""
    state, out, now = {}, [], start
    for hosts in hosts_seq:
        v = W.verdict(hosts, True, UP, state, now)
        state = W.next_state(v, state, now)
        out.append(v)
        now += step
    return out, state


class Sequences(unittest.TestCase):
    def test_2026_09_24_incident_restarts_on_second_zero_then_recovers(self):
        out, state = run_ticks([1, 0, 0, 1])
        self.assertEqual(out, ["ok", "suspect", "restart", "ok"])
        self.assertEqual(state, {})

    def test_one_blip_is_not_restarted(self):
        out, _ = run_ticks([1, 0, 1, 0, 1])
        self.assertNotIn("restart", out)

    def test_restart_that_does_not_help_is_stuck_then_retried(self):
        out, _ = run_ticks([0, 0, 0, 0, 0, 0])
        # restart; the next 0 falls within GRACE_S of it → stuck until grace passes.
        self.assertEqual(out[:3], ["suspect", "restart", "stuck"])
        self.assertIn("restart", out[3:])

    def test_stuck_alert_flag_survives_retries_until_healthy(self):
        state = W.next_state("restart", {"stuck_alerted": True, "zero_since": 1.0}, 50.0)
        self.assertTrue(state["stuck_alerted"])
        self.assertEqual(W.next_state("ok", state, 60.0), {})

    def test_query_failure_needs_two_ticks(self):
        out, _ = run_ticks([None, None, None, 1])
        self.assertEqual(out, ["unknown", "query_failed", "query_failed", "ok"])


class MainSideEffects(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, val in (("STATE_PATH", Path(tmp.name) / "s.json"),):
            p = mock.patch.object(W, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.calls = []
        self.notes = []

        def fake_run(args, timeout=30):
            self.calls.append(args)
            return mock.Mock(returncode=0, stdout="")
        for name, val in (("_run", fake_run), ("unit_active", lambda: True),
                          ("unit_uptime_s", lambda: UP),
                          ("notify", lambda *a, **k: self.notes.append(a))):
            p = mock.patch.object(W, name, val)
            p.start()
            self.addCleanup(p.stop)

    def tick(self, hosts, *argv):
        with mock.patch.object(W, "query_hosts", return_value=hosts), \
             mock.patch.object(sys, "argv", ["w", *argv]), \
             mock.patch("builtins.print"):
            W.main()

    def restarts(self):
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", "restart"]]

    def test_second_zero_restarts_the_unit(self):
        self.tick(0)
        self.assertEqual(self.restarts(), [])
        self.tick(0)
        self.assertEqual(self.restarts(), [["systemctl", "--user", "restart", W.UNIT]])
        self.assertEqual(self.notes, [])

    def test_dry_run_mutates_nothing(self):
        self.tick(0)
        before = W.STATE_PATH.read_text()
        self.tick(0, "--dry-run")
        self.assertEqual(self.restarts(), [])
        self.assertEqual(W.STATE_PATH.read_text(), before)

    def test_query_failure_alerts_once(self):
        for _ in range(4):
            self.tick(None)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.restarts(), [])
