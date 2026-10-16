"""Tests for scripts/health_report.py — the weekly automation health checks.

The six judge_* functions are pure (no IO); this file pins their ok/warn/skip
boundaries against the incidents they were built to re-detect, plus the
lstart/watchdog/equity parsers, the byte-cursor log tailer, and the episode/
kakao renderers. Silent-when-healthy is load-bearing: kakao text names WARN
checks only, and a 'skip' alone never flips the overall status to warn.

scripts/ has no __init__.py, so the module is loaded by path via importlib —
its top level is constants/defs only (entry point strictly under __main__).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from todo_system.kakao_format import KAKAO_TEXT_LIMIT
from todo_system.orchestrator import dashboard
from todo_system.orchestrator.assignment import Assignment

VAULT = Path(__file__).resolve().parents[2]
_SRC = VAULT / "scripts" / "health_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("health_report", _SRC)
    mod = importlib.util.module_from_spec(spec)
    # sys.modules registration must precede exec: @dataclass resolves the
    # module's namespace via sys.modules[cls.__module__] for its (stringified,
    # future-annotations) field types.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


H = _load()

KST = dt.timezone(dt.timedelta(hours=9))
GIB = 1024 ** 3


def k(*args) -> dt.datetime:
    """Aware KST datetime shorthand."""
    return dt.datetime(*args, tzinfo=KST)


# The first Sunday 08:30 after the incident week (07-27~31 are the weekdays
# behind it) — the judgment functions take `now` injected, so every case below
# is deterministic regardless of when/where the suite runs (CI is UTC).
SUNDAY = k(2026, 8, 2, 8, 30)

# verbatim from Worker/assignments/macp-dashboard-public-access.json — the same
# string as test_orchestrator.MergedButBlockedTest.BLOCKER. A real blocker is a
# ~300-char sentence, which is why the ledger detail must name slugs only.
PARK_BLOCKER = ("AC4/AC5 need a human on macp-hq: sudo-enable Tailscale Funnel "
                "(operator/sudo required — a forbidden hard rule, and `tailscale` is "
                "not in the verify whitelist), then from the phone with Tailscale OFF "
                "confirm no-token→401 / token→200 on the public DNS host. The code "
                "half (AC1–AC3, AC6) is complete and green.")


class DaemonStaleness(unittest.TestCase):
    CODE_TIME = k(2026, 7, 29, 18, 0)

    def test_daemon_started_after_code_ok(self):
        r = H.judge_daemon_staleness({"com.x.runner": k(2026, 7, 30, 9, 0)}, self.CODE_TIME)
        self.assertEqual(r.level, "ok")

    def test_daemon_started_before_code_warns(self):
        # the 07-24~29 incident: runner process predates the merged fix
        r = H.judge_daemon_staleness({"com.x.runner": k(2026, 7, 24, 9, 0)}, self.CODE_TIME)
        self.assertEqual(r.level, "warn")
        self.assertIn("com.x.runner", r.detail)

    def test_only_stale_daemons_named(self):
        r = H.judge_daemon_staleness(
            {"fresh-agent": k(2026, 7, 30, 9, 0), "stale-agent": k(2026, 7, 20, 9, 0)},
            self.CODE_TIME,
        )
        self.assertEqual(r.level, "warn")
        self.assertIn("stale-agent", r.detail)
        self.assertNotIn("fresh-agent", r.detail)

    def test_missing_code_time_skips(self):
        self.assertEqual(H.judge_daemon_staleness({}, None).level, "skip")

    def test_no_live_pids_ok(self):
        self.assertEqual(H.judge_daemon_staleness({}, self.CODE_TIME).level, "ok")


class StaleDaemonRule(unittest.TestCase):
    """The single owner of the staleness rule.

    todo_system/worker.py restarts resident daemons on this function's output
    instead of re-deriving the comparison. Two copies of the rule is exactly the
    accident the check exists to catch — one side gets fixed and the other does
    not — so these edges are the restart decision's edges as well.
    """

    CODE_TIME = k(2026, 7, 29, 18, 0)

    def test_lists_only_daemons_started_before_the_code(self):
        starts = {"old": k(2026, 7, 24, 9, 0), "new": k(2026, 7, 30, 9, 0)}
        self.assertEqual(H.stale_daemons(starts, self.CODE_TIME), ["old"])

    def test_output_is_sorted_so_it_is_stable(self):
        starts = {"z": k(2026, 7, 20, 9, 0), "a": k(2026, 7, 21, 9, 0)}
        self.assertEqual(H.stale_daemons(starts, self.CODE_TIME), ["a", "z"])

    def test_empty_when_nothing_is_resident(self):
        self.assertEqual(H.stale_daemons({}, self.CODE_TIME), [])

    def test_a_daemon_started_exactly_at_the_commit_is_not_stale(self):
        # boundary is `started < code_time`: equal means it already has the code
        self.assertEqual(H.stale_daemons({"a": self.CODE_TIME}, self.CODE_TIME), [])

    def test_comparison_survives_a_different_timezone(self):
        # worker.py hands these in as UTC-aware epochs while git reports +09:00
        utc = dt.timezone.utc
        started = k(2026, 7, 24, 9, 0).astimezone(utc)
        self.assertEqual(H.stale_daemons({"old": started}, self.CODE_TIME), ["old"])

    def test_the_weekly_judge_names_exactly_what_the_rule_returns(self):
        # if these ever disagree, the warning and the restart have diverged
        starts = {"fresh": k(2026, 7, 30, 9, 0), "stale": k(2026, 7, 20, 9, 0)}
        detail = H.judge_daemon_staleness(starts, self.CODE_TIME).detail
        self.assertEqual(H.stale_daemons(starts, self.CODE_TIME), ["stale"])
        self.assertIn("stale", detail)
        self.assertNotIn("fresh", detail)

    def test_the_code_paths_are_defined_here_and_only_here(self):
        self.assertEqual(H.CODE_TIME_PATHS, ("todo_system", "scripts"))


class Ledger(unittest.TestCase):
    @staticmethod
    def row(slug="a", state="awaiting_human", pr_state=None, error=None, blocked_on=""):
        return {"slug": slug, "state": state, "pr_state": pr_state, "error": error,
                "blocked_on": blocked_on}

    def test_no_rows_ok(self):
        self.assertEqual(H.judge_ledger([]).level, "ok")

    def test_open_pr_ok(self):
        self.assertEqual(H.judge_ledger([self.row(pr_state="OPEN")]).level, "ok")

    def test_no_pr_found_ok(self):
        # a failed-before-push task legitimately has no PR — not a false ledger
        self.assertEqual(H.judge_ledger([self.row(state="failed")]).level, "ok")

    def test_merged_but_still_open_in_ledger_warns(self):
        r = H.judge_ledger([self.row(slug="ghosted", pr_state="MERGED")])
        self.assertEqual(r.level, "warn")
        self.assertIn("ghosted", r.detail)

    def test_gh_failure_is_loud(self):
        # 점검 불능 = the reconciler's own gh dependency may be dead → warn, not silence
        r = H.judge_ledger([self.row(error="gh 미설치")])
        self.assertEqual(r.level, "warn")
        self.assertIn("점검 불능", r.detail)

    def test_merged_blocked_park_is_not_a_false_ledger(self):
        # PR #132: reconcile_merged parks a merged row whose Verify named a step
        # still owed by a human (blocked_on) instead of closing it. The ledger is
        # telling the truth there — a human is waiting — so it must not page; it
        # is named on its own 사람 대기 line instead.
        r = H.judge_ledger([self.row(slug="dash-park", pr_state="MERGED",
                                     blocked_on=PARK_BLOCKER)])
        self.assertEqual(r.level, "ok")
        self.assertIn("사람 대기(park", r.detail)
        self.assertIn("dash-park", r.detail)
        self.assertNotIn("원장 미정리", r.detail)
        self.assertNotIn("\n", r.detail)             # one episode table cell
        self.assertNotIn(PARK_BLOCKER, r.detail)     # slugs only, not the blocker prose
        # ok here ⇒ episode status ok ⇒ main() sends no kakao (정상이면 무음)
        self.assertIn("status: ok", H.render_episode([r], SUNDAY))

    def test_park_and_real_lie_warn_only_for_the_lie(self):
        r = H.judge_ledger([
            self.row(slug="dash-park", pr_state="MERGED", blocked_on=PARK_BLOCKER),
            self.row(slug="ghosted", pr_state="MERGED"),
        ])
        self.assertEqual(r.level, "warn")
        self.assertIn("사람 대기(park", r.detail)    # before the splits below index into it
        lie = r.detail.split("머지됐는데 원장 미정리:")[1].split(" / ")[0]
        self.assertIn("ghosted", lie)
        self.assertNotIn("dash-park", lie)
        self.assertIn("dash-park", r.detail.split("사람 대기(park")[1])

    def test_merged_without_blocker_still_warns(self):
        # nothing left for a human, yet still parked: the ledger is lying
        r = H.judge_ledger([self.row(slug="ghosted", pr_state="MERGED", blocked_on="")])
        self.assertEqual(r.level, "warn")
        self.assertIn("머지됐는데 원장 미정리: ghosted", r.detail)
        self.assertNotIn("사람 대기(park", r.detail)

    def test_failed_merged_with_blocker_still_warns(self):
        # reconcile_merged re-parks a FAILED merged+blocked row on every tick, so
        # one still sitting in failed means the reconciler is not running — the
        # very thing this check exists to catch. Only awaiting_human is exempt.
        r = H.judge_ledger([self.row(slug="stuck", state="failed", pr_state="MERGED",
                                     blocked_on=PARK_BLOCKER)])
        self.assertEqual(r.level, "warn")
        self.assertIn("머지됐는데 원장 미정리:", r.detail)
        lie = r.detail.split("머지됐는데 원장 미정리:")[1].split(" / ")[0]
        self.assertIn("stuck", lie)
        self.assertNotIn("사람 대기(park", r.detail)


class LedgerCollector(unittest.TestCase):
    """gather_ledger_rows → judge_ledger end to end, over ledger JSON written by
    the real Assignment.to_json(), with `gh` faked. A judge-only fix passes the
    Ledger cases above and changes nothing in the weekly run if the collector
    never carries `blocked_on` onto the row."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        park = Assignment(task_id="dash-park", agent_id="w", corr_id="c",
                          state="awaiting_human", kind="ai-local",
                          blocked_on=PARK_BLOCKER, last_alert_kind="blocked",
                          history=[{"ts": "2026-09-18T07:00:00+09:00",
                                    "type": dashboard._MERGED_BUT_BLOCKED,
                                    "from": "orchestrator"}],
                          payload={"branch": "worker/dash-park"})
        ghosted = Assignment(task_id="ghosted", agent_id="w", corr_id="c",
                             state="awaiting_human", kind="ai-local")
        closed = Assignment(task_id="closed", agent_id="w", corr_id="c",
                            state="done", kind="ai-local")
        for a in (park, ghosted, closed):
            (self.dir / f"{a.task_id}.json").write_text(a.to_json(), encoding="utf-8")

    def test_blocker_reaches_the_judge(self):
        looked_up = []

        def fake_gh(argv, **kw):
            looked_up.append(tuple(argv[:4]))
            return subprocess.CompletedProcess(argv, 0, '{"state": "MERGED"}', "")

        with mock.patch.object(H.subprocess, "run", side_effect=fake_gh) as run:
            rows = H.gather_ledger_rows(self.dir)
        self.assertEqual(run.call_count, 2)          # the done row is never looked up
        self.assertEqual(sorted(looked_up), [("gh", "pr", "view", "worker/dash-park"),
                                             ("gh", "pr", "view", "worker/ghosted")])
        by_slug = {r["slug"]: r for r in rows}
        self.assertEqual(by_slug["dash-park"].get("blocked_on"), PARK_BLOCKER)
        self.assertEqual(by_slug["ghosted"].get("blocked_on"), "")
        r = H.judge_ledger(rows)
        self.assertEqual(r.level, "warn")
        self.assertIn("사람 대기(park", r.detail)
        lie = r.detail.split("머지됐는데 원장 미정리:")[1].split(" / ")[0]
        self.assertIn("ghosted", lie)
        self.assertNotIn("dash-park", lie)
        self.assertIn("dash-park", r.detail.split("사람 대기(park")[1])


class EquityFreshness(unittest.TestCase):
    def test_calendar_assumptions(self):
        self.assertEqual(dt.date(2026, 8, 2).weekday(), 6)  # Sunday
        self.assertEqual(dt.date(2026, 7, 31).weekday(), 4)  # Friday

    def test_friday_data_ok(self):
        r = H.judge_equity_freshness([k(2026, 7, 31, 14, 55)], SUNDAY)
        self.assertEqual(r.level, "ok")

    def test_single_holiday_stays_silent(self):
        # Friday holiday (no data), Thursday traded → gap 1 < 2
        r = H.judge_equity_freshness([k(2026, 7, 30, 11, 0)], SUNDAY)
        self.assertEqual(r.level, "ok")

    def test_two_weekday_gap_warns(self):
        # last in-hours point Wednesday → Fri+Thu empty
        r = H.judge_equity_freshness([k(2026, 7, 29, 11, 0)], SUNDAY)
        self.assertEqual(r.level, "warn")

    def test_five_day_silence_incident_warns(self):
        # the 07-25~29 class: last point the previous Friday
        r = H.judge_equity_freshness([k(2026, 7, 24, 15, 0)], SUNDAY)
        self.assertEqual(r.level, "warn")

    def test_after_hours_points_do_not_count(self):
        points = [k(2026, 7, 31, 16, 0), k(2026, 7, 30, 8, 30), k(2026, 7, 29, 11, 0)]
        r = H.judge_equity_freshness(points, SUNDAY)
        self.assertEqual(r.level, "warn")  # Fri/Thu have only out-of-hours data

    def test_no_points_warns(self):
        self.assertEqual(H.judge_equity_freshness([], SUNDAY).level, "warn")

    def test_todays_open_window_not_judged(self):
        # Wed 10:00 run: today's window hasn't closed — yesterday's data keeps it ok
        r = H.judge_equity_freshness([k(2026, 7, 28, 11, 0)], k(2026, 7, 29, 10, 0))
        self.assertEqual(r.level, "ok")


class WatchdogAlerts(unittest.TestCase):
    def test_ten_per_day_ok(self):
        alerts = [k(2026, 7, 30, 9, m) for m in range(10)]
        self.assertEqual(H.judge_watchdog_alerts(alerts, SUNDAY).level, "ok")

    def test_eleven_on_report_day_warns(self):
        # A storm on the day the report runs is recurrence -> page.
        alerts = [k(2026, 8, 2, 9, m) for m in range(11)]
        r = H.judge_watchdog_alerts(alerts, SUNDAY)
        self.assertEqual(r.level, "warn")
        self.assertIn("2026-08-02", r.detail)

    def test_135_per_day_recent_incident_warns(self):
        alerts = [k(2026, 8, 2, 9 + m // 60, m % 60) for m in range(135)]
        r = H.judge_watchdog_alerts(alerts, SUNDAY)
        self.assertEqual(r.level, "warn")
        self.assertIn("135", r.detail)

    def test_past_storm_inside_window_does_not_page(self):
        # Regression (2026-07-30): the threshold/period storm was diagnosed and
        # fixed the same day, yet the Sunday run would have re-warned on it for a
        # week. A noisy day older than warn_recent_days is history, not
        # recurrence — informational "ok", never a page.
        alerts = [k(2026, 7, 30, 9 + m // 60, m % 60) for m in range(135)]
        r = H.judge_watchdog_alerts(alerts, SUNDAY)   # SUNDAY = 2026-08-03
        self.assertEqual(r.level, "ok")
        self.assertIn("과거 폭주 이력", r.detail)

    def test_alerts_outside_window_ignored(self):
        alerts = [k(2026, 7, 20, 9, m) for m in range(50)]  # 13 days before SUNDAY
        self.assertEqual(H.judge_watchdog_alerts(alerts, SUNDAY).level, "ok")


class LogErrors(unittest.TestCase):
    def test_below_threshold_ok(self):
        self.assertEqual(H.judge_log_errors({"a.err.log": 9}).level, "ok")

    def test_at_threshold_warns(self):
        r = H.judge_log_errors({"a.err.log": 10, "b.out.log": 0})
        self.assertEqual(r.level, "warn")
        self.assertIn("a.err.log", r.detail)
        self.assertNotIn("b.out.log", r.detail)

    def test_no_files_ok(self):
        self.assertEqual(H.judge_log_errors({}).level, "ok")


class DiskFree(unittest.TestCase):
    """(6) One number, two readers: the run-start guard cleans below
    disk_guard.MIN_FREE_BYTES, and this check warns below the same line — the
    09-19 class (160M left, every Android run dead of ENOSPC)."""

    def test_at_the_guard_threshold_ok(self):
        r = H.judge_disk_free((H.disk_guard.MIN_FREE_BYTES, 48 * GIB))
        self.assertEqual(r.level, "ok")
        self.assertIn("임계 3.0G", r.detail)

    def test_one_byte_below_warns_and_names_the_human_step(self):
        r = H.judge_disk_free((H.disk_guard.MIN_FREE_BYTES - 1, 48 * GIB))
        self.assertEqual(r.level, "warn")
        self.assertIn("48.0G", r.detail)
        self.assertIn("디스크 증설", r.detail)

    def test_the_incident_level_warns(self):
        r = H.judge_disk_free((160 * 1024 ** 2, 48 * GIB))
        self.assertEqual(r.level, "warn")
        self.assertIn("여유 0.2G", r.detail)

    def test_unreadable_skips(self):
        self.assertEqual(H.judge_disk_free(None).level, "skip")

    def test_default_threshold_is_the_guards_read_at_call_time(self):
        self.assertEqual(H.disk_guard.MIN_FREE_BYTES, 3 * GIB)
        with mock.patch.object(H.disk_guard, "MIN_FREE_BYTES", 10):
            self.assertEqual(H.judge_disk_free((9, 100)).level, "warn")
            self.assertEqual(H.judge_disk_free((10, 100)).level, "ok")

    def test_an_explicit_threshold_wins(self):
        r = H.judge_disk_free((5 * GIB, 48 * GIB), threshold=6 * GIB)
        self.assertEqual(r.level, "warn")

    def test_collector_uses_the_guards_measurement(self):
        seen = []

        def fake(path):
            seen.append(path)
            return SimpleNamespace(free=2 * GIB, total=48 * GIB, used=46 * GIB)

        self.assertEqual(H.gather_disk_usage(disk_usage=fake), (2 * GIB, 48 * GIB))
        self.assertEqual(len(seen), 1)

    def test_collector_error_is_none(self):
        self.assertIsNone(H.gather_disk_usage(disk_usage=mock.Mock(side_effect=OSError(5, "EIO"))))

    def test_episode_lists_the_incident(self):
        text = H.render_episode([H.CheckResult(H.CHECK_DISK, "warn", "여유 0.2G")], SUNDAY)
        self.assertIn(f"| {H.CHECK_DISK} | ⚠️ warn |", text)
        self.assertIn(f"| {H.CHECK_DISK} | 루트 디스크 포화", text)
        self.assertIn("ENOSPC", text)
        self.assertIn("status: warn", text)


class RunChecksWiring(unittest.TestCase):
    """run_checks with every collector faked: six rows, disk last, and a disk
    collector crash isolated to its own skip."""

    def _run(self, **overrides):
        fakes = {
            "gather_daemon_starts": mock.Mock(return_value={}),
            "gather_code_time": mock.Mock(return_value=None),
            "gather_ledger_rows": mock.Mock(return_value=[]),
            "read_equity_points": mock.Mock(return_value=[]),
            "read_watchdog_alerts": mock.Mock(return_value=[]),
            "tail_new_error_counts": mock.Mock(return_value=({}, {})),
            "gather_disk_usage": mock.Mock(return_value=(1 * GIB, 48 * GIB)),
            **overrides,
        }
        with contextlib.ExitStack() as stack:
            for name, fake in fakes.items():
                stack.enter_context(mock.patch.object(H, name, fake))
            results, _ = H.run_checks(SUNDAY, {})
        return results

    def test_six_checks_with_disk_last(self):
        results = self._run()
        self.assertEqual([r.name for r in results],
                         [H.CHECK_DAEMON, H.CHECK_LEDGER, H.CHECK_FRESHNESS,
                          H.CHECK_WATCHDOG, H.CHECK_LOGS, H.CHECK_DISK])
        self.assertEqual(results[-1].level, "warn")
        self.assertEqual([n for n, _ in H.INCIDENT_NOTES], [r.name for r in results])

    def test_a_disk_collector_crash_is_a_skip_not_a_dead_report(self):
        results = self._run(gather_disk_usage=mock.Mock(side_effect=RuntimeError("statvfs")))
        self.assertEqual((results[-1].name, results[-1].level), (H.CHECK_DISK, "skip"))
        self.assertEqual(len(results), 6)


class BarePythonImport(unittest.TestCase):
    def test_the_import_chain_stays_stdlib(self):
        # launchd/systemd run this under a bare python3 with no venv, so no yaml:
        # the disk_guard import must not drag anything third-party in.
        code = ("import importlib.util, sys\n"
                f"s = importlib.util.spec_from_file_location('hr', {str(_SRC)!r})\n"
                "m = importlib.util.module_from_spec(s)\n"
                "sys.modules['hr'] = m\n"
                "s.loader.exec_module(m)\n"
                "print('yaml' in sys.modules, m.disk_guard.MIN_FREE_BYTES)\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["False", str(3 * GIB)])


class Parsers(unittest.TestCase):
    def test_lstart_basic(self):
        parsed = H._parse_lstart("Thu Jul 30 08:15:00 2026\n")
        self.assertEqual(parsed.replace(tzinfo=None), dt.datetime(2026, 7, 30, 8, 15, 0))

    def test_lstart_padded_day_of_month(self):
        parsed = H._parse_lstart("Thu Jul  2 09:05:07 2026")
        self.assertEqual(parsed.replace(tzinfo=None), dt.datetime(2026, 7, 2, 9, 5, 7))

    def test_lstart_is_aware(self):
        self.assertIsNotNone(H._parse_lstart("Thu Jul 30 08:15:00 2026").tzinfo)

    def test_lstart_garbage_none(self):
        self.assertIsNone(H._parse_lstart(""))
        self.assertIsNone(H._parse_lstart("not a date"))

    def test_watchdog_alert_line_parsed(self):
        ts = H._parse_watchdog_line("[2026-07-30T09:00:00+09:00] ALERT: equity stale")
        self.assertEqual(ts, k(2026, 7, 30, 9, 0))

    def test_watchdog_heartbeat_ignored(self):
        line = "[2026-07-30T15:45:48+09:00] heartbeat fresh (phase=sleep, ts=...)"
        self.assertIsNone(H._parse_watchdog_line(line))

    def test_equity_rows_skip_header_and_broken(self):
        text = "timestamp,equity\n2026-07-30T15:02:50.473153+09:00,9909060.0\n\nbroken,1\n"
        points = H._parse_equity_rows(text)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].date(), dt.date(2026, 7, 30))

    def test_count_stream_errors(self):
        text = (
            "2026-07-30 ok line\n"
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1, in <module>\n'
            "NameError: name 'x' is not defined\n"
            "[ts] ERROR something\n"
            "[ts] ERROR other\n"
        )
        # 1 traceback header + 2 ERROR lines (the NameError line itself is not
        # double-counted — lowercase 'Error' doesn't match)
        self.assertEqual(H._count_stream_errors(text), 3)


class LogCursor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "svc.err.log"

    def test_first_observation_is_baseline(self):
        self.log.write_text("ERROR old1\nERROR old2\n")
        counts, cursors = H.tail_new_error_counts(self.tmp, {})
        self.assertEqual(counts["svc.err.log"], 0)  # pre-existing noise must not fire
        self.assertEqual(cursors["svc.err.log"], self.log.stat().st_size)

    def test_new_chunk_counted(self):
        self.log.write_text("ok\n")
        _, cursors = H.tail_new_error_counts(self.tmp, {})
        with open(self.log, "a", encoding="utf-8") as f:
            f.write("ERROR one\nERROR two\nok\n")
        counts, cursors2 = H.tail_new_error_counts(self.tmp, cursors)
        self.assertEqual(counts["svc.err.log"], 2)
        self.assertEqual(cursors2["svc.err.log"], self.log.stat().st_size)

    def test_unchanged_file_counts_zero(self):
        self.log.write_text("ERROR one\n")
        _, cursors = H.tail_new_error_counts(self.tmp, {})
        counts, _ = H.tail_new_error_counts(self.tmp, cursors)
        self.assertEqual(counts["svc.err.log"], 0)

    def test_truncated_file_rescans_from_zero(self):
        self.log.write_text("x" * 100 + "\n")
        _, cursors = H.tail_new_error_counts(self.tmp, {})
        self.log.write_text("ERROR after rotate\n")  # smaller than stored cursor
        counts, _ = H.tail_new_error_counts(self.tmp, cursors)
        self.assertEqual(counts["svc.err.log"], 1)

    def test_out_log_also_scanned(self):
        (self.tmp / "svc.out.log").write_text("hello\n")
        counts, _ = H.tail_new_error_counts(self.tmp, {})
        self.assertIn("svc.out.log", counts)


class Renderers(unittest.TestCase):
    @staticmethod
    def results(first_level):
        return [
            H.CheckResult(H.CHECK_DAEMON, first_level, "detail-a"),
            H.CheckResult(H.CHECK_LEDGER, "ok", "detail-b"),
        ]

    def test_episode_warn_status(self):
        text = H.render_episode(self.results("warn"), SUNDAY)
        self.assertIn("status: warn", text)
        self.assertIn("⚠️", text)
        self.assertIn(H.CHECK_DAEMON, text)
        self.assertIn("2026-08-02", text)

    def test_episode_ok_status(self):
        self.assertIn("status: ok", H.render_episode(self.results("ok"), SUNDAY))

    def test_skip_alone_does_not_flip_status(self):
        text = H.render_episode(self.results("skip"), SUNDAY)
        self.assertIn("status: ok", text)  # skip is surfaced but is not a WARN
        self.assertIn("점검 불능", text)

    def test_kakao_names_only_warn_checks(self):
        results = [
            H.CheckResult(H.CHECK_DAEMON, "warn", "stale"),
            H.CheckResult(H.CHECK_FRESHNESS, "ok", "fine"),
        ]
        msg = H.build_kakao_message(results)
        self.assertIn(H.CHECK_DAEMON, msg)
        self.assertNotIn(H.CHECK_FRESHNESS, msg)
        self.assertLessEqual(len(msg), KAKAO_TEXT_LIMIT)

    def test_kakao_message_capped(self):
        # 8 long WARN names → the names ride as a clipped tail; the single length
        # source is now todo_system.kakao_format (health_report no longer defines
        # its own KAKAO_TEXT_LIMIT), and the message is self-limiting by construction.
        results = [H.CheckResult("점검-" + "x" * 40, "warn", "d") for _ in range(8)]
        self.assertLessEqual(len(H.build_kakao_message(results)), KAKAO_TEXT_LIMIT)


if __name__ == "__main__":
    unittest.main()
