"""Detached long jobs — work that has to outlive the stage that starts it.

These drive real processes rather than mocks, because the whole point of the
module is a property mocks cannot show: that the job is NOT a child of whoever
started it, and survives that process going away.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from todo_system.orchestrator import jobs
from todo_system.orchestrator.runner import StagedExecutor


def _wait(pred, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = jobs.JOBS_DIR
        jobs.JOBS_DIR = self.tmp

    def tearDown(self):
        jobs.stop("t")
        jobs.JOBS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_absent_before_anything_starts(self):
        self.assertEqual(jobs.poll("t")["state"], "absent")

    def test_running_then_done_with_exit_code(self):
        jobs.start("t", "sleep 0.3; exit 0")
        self.assertEqual(jobs.poll("t")["state"], "running")
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"))
        self.assertEqual(jobs.poll("t")["rc"], 0)

    def test_nonzero_exit_is_failed(self):
        jobs.start("t", "exit 3")
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "failed"))
        self.assertEqual(jobs.poll("t")["rc"], 3)

    def test_killed_without_an_exit_code_is_failed_not_done(self):
        # The dangerous case: a truncated collection must never read as success.
        jobs.start("t", "sleep 30")
        pid = jobs.read("t")["pid"]
        # never let a recorded pid reach kill unchecked: -1 is not "no process",
        # it is every process this user owns.
        self.assertGreater(pid, 0)
        os.kill(pid, 9)
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "failed"))
        self.assertIsNone(jobs.poll("t")["rc"])

    def test_stdout_is_captured_to_the_job_log(self):
        jobs.start("t", "echo collected 100/100")
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"))
        self.assertIn("collected 100/100",
                      jobs.log_path("t").read_text(encoding="utf-8"))

    def test_runs_in_the_requested_directory(self):
        work = self.tmp / "work"
        work.mkdir()
        jobs.start("t", "pwd", cwd=work)
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"))
        self.assertIn(str(work.resolve()),
                      jobs.log_path("t").read_text(encoding="utf-8"))

    def test_job_outlives_the_process_that_started_it(self):
        """The property the whole module exists for. A child would die with its
        parent — which is exactly how ai-stock-trading-8b lost 85 tickers."""
        code = (
            "import sys, time;"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r});"
            "from todo_system.orchestrator import jobs;"
            "from pathlib import Path;"
            f"jobs.JOBS_DIR = Path({str(self.tmp)!r});"
            "jobs.start('t', 'sleep 5; printf done > " + str(self.tmp / "marker") + "')"
        )
        subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
        # the starter has exited; the job must still be alive and still finish
        self.assertEqual(jobs.poll("t")["state"], "running")
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done", timeout=15))
        self.assertEqual((self.tmp / "marker").read_text(encoding="utf-8"), "done")

    def test_no_scope_outside_a_systemd_unit(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INVOCATION_ID", None)
            self.assertEqual(jobs._scope_prefix(), [])

    def test_failed_scope_launch_falls_back_to_a_plain_one(self):
        """systemd-run without a reachable user bus exits non-zero and runs
        nothing. That must not leave the task with no job at all."""
        with mock.patch.object(jobs, "_scope_prefix", return_value=["false"]):
            st = jobs.start("t", "exit 0")
        self.assertGreater(st["pid"], 0)
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"))

    @unittest.skipUnless(sys.platform.startswith("linux")
                         and os.environ.get("INVOCATION_ID")
                         and shutil.which("systemd-run"),
                         "needs to run inside a systemd unit")
    def test_job_leaves_the_starters_cgroup_under_systemd(self):
        """The runner unit is KillMode=control-group: stopping it kills every
        process in its cgroup, and a new session does not take the job out
        of that cgroup. On 2026-10-08 a runner restart for new code killed
        an ai-stock-trading sweep twice that way. The job has to be in some
        other cgroup."""
        jobs.start("t", "sleep 30")
        pid = jobs.read("t")["pid"]
        self.assertGreater(pid, 0)

        def cg(p):
            return Path(f"/proc/{p}/cgroup").read_text(encoding="utf-8").strip()
        self.assertTrue(_wait(lambda: Path(f"/proc/{pid}/cgroup").exists()))
        self.assertNotEqual(cg(pid), cg(os.getpid()))
        self.assertIn(".scope", cg(pid))

    def test_stop_refuses_a_pid_that_is_not_one_process(self):
        """os.kill's first argument is overloaded and -1 means "every process
        this uid may signal". `start()` records -1 when the pid file is
        unreadable, so `stop()` can be handed it by ordinary means — on
        2026-09-10 a teardown did exactly that and SIGTERM'd the whole login
        session. os.kill is mocked here so a regression fails the test rather
        than the machine."""
        for bad in (-1, 0, -42, "", None, "abc"):
            jobs.clear("t")
            (self.tmp / "t.json").write_text(
                json.dumps({"slug": "t", "pid": bad}), encoding="utf-8")
            with mock.patch.object(jobs.os, "kill") as killed:
                self.assertFalse(jobs.stop("t"), f"stop() accepted pid {bad!r}")
                killed.assert_not_called()

    def test_a_job_that_never_launched_is_failed_not_running(self):
        """Same -1, quieter symptom: `kill(-1, 0)` succeeds, so an unguarded
        liveness probe reads a job that never started as running, forever."""
        (self.tmp / "t.json").write_text(
            json.dumps({"slug": "t", "pid": -1}), encoding="utf-8")
        self.assertEqual(jobs.poll("t")["state"], "failed")

    def test_the_no_probe_fixture_does_not_reach_this_module(self):
        """conftest fakes model_select's CLI probe. If it ever patches the
        subprocess MODULE again instead of the name inside model_select, the
        fake lands here too: start() launches nothing and records pid -1. This
        is the tripwire for that, one assert away from the teardown that fires
        the kill."""
        st = jobs.start("t", "sleep 30")
        self.assertGreater(st["pid"], 0)

    def test_clear_forgets_the_job(self):
        jobs.start("t", "exit 1")
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "failed"))
        jobs.clear("t")
        self.assertEqual(jobs.poll("t")["state"], "absent")


class LongJobGateTest(unittest.TestCase):
    """The runner must not spend a single agent turn while the job is unfinished."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = jobs.JOBS_DIR
        jobs.JOBS_DIR = self.tmp

    def tearDown(self):
        jobs.stop("t")
        jobs.JOBS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gate(self, cmd="sleep 30"):
        return StagedExecutor._long_job_gate(
            "t", {"long_job": cmd}, str(self.tmp), lambda *_: None)

    def test_first_dispatch_starts_the_job_and_defers(self):
        out = self._gate()
        self.assertFalse(out.ok)
        self.assertTrue(out.payload["job_running"])
        self.assertIn("started", out.summary)
        self.assertEqual(jobs.poll("t")["state"], "running")

    def test_later_dispatch_polls_without_restarting(self):
        first = jobs.start("t", "sleep 30")["pid"]
        out = self._gate()
        self.assertTrue(out.payload["job_running"])
        self.assertEqual(jobs.read("t")["pid"], first)   # same job, not a new one

    def test_gate_opens_once_the_job_succeeds(self):
        jobs.start("t", "exit 0")
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"))
        self.assertIsNone(self._gate())      # None = run the pipeline

    def test_done_job_is_consumed_once_not_re_promoted_every_dispatch(self):
        """A finished long_job must open the gate exactly ONCE. The `.rc` file
        makes poll return `done` for as long as it lingers, so if the done
        branch does not clear the job, EVERY later dispatch re-runs the full
        pipeline (branch-prep = git reset --hard + git clean -fd) against the
        same completed data — the ai-stock-trading-13 triple-run incident. The
        second dispatch must defer, not re-promote."""
        jobs.start("t", "exit 0")
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "done"))
        self.assertIsNone(self._gate())               # 1st: consume, run pipeline once
        self.assertNotEqual(jobs.poll("t")["state"], "done")  # done must be consumed
        out = self._gate()                            # 2nd dispatch must NOT re-promote
        self.assertIsNotNone(out)                     # defers instead of re-running pipeline
        self.assertTrue(out.payload.get("job_running"))

    def test_failed_job_is_a_real_failure_and_is_cleared_for_retry(self):
        jobs.start("t", "exit 2")
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] == "failed"))
        out = self._gate()
        self.assertFalse(out.ok)
        self.assertNotIn("job_running", out.payload)   # must NOT defer forever
        self.assertIn("rc=2", out.summary)
        self.assertEqual(jobs.poll("t")["state"], "absent")   # next attempt re-runs



class NoProbeFixtureTest(unittest.TestCase):
    """The autouse fixture must fake model_select's probe — and only that."""

    def test_model_select_sees_the_fake(self):
        from todo_system.orchestrator import model_select as ms
        self.assertEqual(ms.subprocess.run(["claude", "-p", "ok"]).stdout, "ok")

    def test_the_real_subprocess_still_launches_processes(self):
        out = subprocess.run([sys.executable, "-c", "print('real')"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "real")


class LongJobSyncsDefaultBranchTest(unittest.TestCase):
    """2026-10-05: the gate started a job on a clone left one merge behind
    origin/main, so the script the previous PR added was missing (rc=2 x3)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = jobs.JOBS_DIR
        jobs.JOBS_DIR = self.tmp / "jobs"

        def git(cwd, *a):
            subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True,
                           env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        self.git = git
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "a.txt").write_text("a")
        git(self.origin, "add", "a.txt")
        git(self.origin, "commit", "-q", "-m", "a")
        self.clone = self.tmp / "clone"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.clone))
        # the previous run's PR lands on origin AFTER the clone last synced
        (self.origin / "job.sh").write_text("exit 0\n")
        git(self.origin, "add", "job.sh")
        git(self.origin, "commit", "-q", "-m", "add job")

    def tearDown(self):
        jobs.stop("t")
        jobs.JOBS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gate(self, sync):
        return StagedExecutor._long_job_gate(
            "t", {"long_job": "sh job.sh"}, str(self.clone), lambda *_: None,
            sync_repo=self.clone if sync else None)

    def test_clone_on_default_branch_is_fast_forwarded_before_start(self):
        self._gate(sync=True)
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] in ("done", "failed")))
        self.assertEqual(jobs.poll("t")["state"], "done")

    def test_clone_on_a_worker_branch_is_left_alone(self):
        self.git(self.clone, "checkout", "-q", "-b", "worker/x")
        self._gate(sync=True)
        self.assertTrue(_wait(lambda: jobs.poll("t")["state"] in ("done", "failed")))
        self.assertEqual(jobs.poll("t")["state"], "failed")
        self.assertFalse((self.clone / "job.sh").exists())
