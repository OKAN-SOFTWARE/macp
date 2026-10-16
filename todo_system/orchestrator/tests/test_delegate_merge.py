"""The delegate lane must honor `auto_merge`.

The 2026-10-12 incident: an `auto_merge: true` delegate run opened a 'Not
auto-merged' PR and then closed its task `done` without ever merging — the
runner's delegate `finally` block ignored `auto_merge` entirely and always
returned `review_required=False`. `done` auto-unblocked the task's downstream via
task_deps. These pin the fix:

  - `merge_pr` / `gh_pr_state` — the new stages helpers (subprocess mocked, no
    real `gh`), both fail-open conservatively.
  - the runner delegate `finally` — auto_merge:true merges and, when the merge
    cannot be confirmed, parks via review_required (never closes done on an open
    PR); auto_merge:false never merges.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor


def _agent(**kw) -> Agent:
    base = dict(id="w", kind="ai-local", capabilities=["git"], repo="~/Worker/x")
    base.update(kw)
    return Agent(**base)


class MergePrHelperTest(unittest.TestCase):
    """`merge_pr` mirrors the Report stage's `gh pr merge --squash
    --delete-branch`, never raises, and reports `merged` True ONLY on rc=0."""

    def test_rc_zero_is_merged_and_uses_squash_delete_branch(self):
        calls = {}

        def fake_run(args, **kw):
            calls["args"] = args
            return subprocess.CompletedProcess(args, 0, "merged", "")

        with mock.patch("subprocess.run", side_effect=fake_run):
            out = stages.merge_pr(Path("/repo"), "worker/t")
        self.assertTrue(out["merged"])
        self.assertIn("--squash", calls["args"])
        self.assertIn("--delete-branch", calls["args"])
        self.assertNotIn("--admin", calls["args"])
        self.assertNotIn("--auto", calls["args"])

    def test_nonzero_rc_is_not_merged(self):
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess(
                            [], 1, "", "Pull request is not mergeable: CONFLICTING")):
            out = stages.merge_pr(Path("/repo"), "worker/t")
        self.assertFalse(out["merged"])
        self.assertIn("CONFLICTING", out["note"])

    def test_gh_absent_is_not_merged_never_raises(self):
        with mock.patch("subprocess.run", side_effect=OSError("no gh")):
            out = stages.merge_pr(Path("/repo"), "worker/t")
        self.assertFalse(out["merged"])

    def test_repo_arg_is_passed_through(self):
        calls = {}

        def fake_run(args, **kw):
            calls["args"] = args
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("subprocess.run", side_effect=fake_run):
            stages.merge_pr(Path("/repo"), "worker/t", repo="o/r")
        self.assertEqual(calls["args"][-2:], ["--repo", "o/r"])


class GhPrStateHelperTest(unittest.TestCase):
    def test_reads_state_upper(self):
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, '{"state":"closed"}', "")):
            self.assertEqual(stages.gh_pr_state("worker/t"), "CLOSED")

    def test_unknown_on_error(self):
        with mock.patch("subprocess.run", side_effect=OSError("no gh")):
            self.assertIsNone(stages.gh_pr_state("worker/t"))
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1, "", "no PR")):
            self.assertIsNone(stages.gh_pr_state("worker/t"))

    def test_unknown_on_missing_state_key(self):
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0, "{}", "")):
            self.assertIsNone(stages.gh_pr_state("worker/t"))


class DelegateFinallyMergeTest(unittest.TestCase):
    """Drive a full VAULT delegate run (repo_url=None, worker_mode=delegate)
    through StagedExecutor so the `finally` publish+merge path actually runs.
    publish_branch and merge_pr are mocked — no real `gh`, no real push."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_dir(self) -> Path:
        dirs = list(self.runs.iterdir())
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def _run_delegate(self, auto_merge, merge_result):
        """Return (outcome, publish_mock, merge_mock) for a vault delegate run."""
        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None

        def fake_stage(argv, **kw):
            # build_stage_argv is stubbed to ["true"]; write the delegate artifact
            # so the stage loop reaches the success return and the finally runs.
            (self._run_dir() / stages.STAGE_ARTIFACT["delegate"]).write_text(
                "# delegate\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        publish = mock.Mock(return_value={"commits": 1, "pushed": True,
                                          "pr_url": "https://gh/pr/1", "note": "opened"})
        merge = mock.Mock(return_value=merge_result)
        payload = {"worker_mode": "delegate", "auto_merge": auto_merge}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_local", return_value=self.tmp / "clone"),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=False),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "cleanup_build_daemons",
                                  return_value={"ran": True, "note": ""}),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_stage),
                mock.patch.object(stages, "publish_branch", publish),
                mock.patch.object(stages, "merge_pr", merge),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("delegate",)),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        return out, publish, merge

    def test_auto_merge_true_merge_success_closes_done(self):
        # The happy path the fix restores: the PR is squash-merged and the run
        # stays a success with NO review_required → collect() closes it done.
        out, publish, merge = self._run_delegate(
            True, {"merged": True, "note": "squash-merged worker/t"})
        self.assertTrue(out.ok, out.summary)
        publish.assert_called_once()
        merge.assert_called_once()
        self.assertFalse(out.payload.get("review_required"))

    def test_auto_merge_true_merge_failure_parks_for_human(self):
        # The core regression: a merge that cannot be confirmed must flip the run
        # to review_required so collect() parks it AWAITING_HUMAN — NOT close done
        # on a still-open PR (the 2026-10-12 incident).
        out, publish, merge = self._run_delegate(
            True, {"merged": False, "note": "gh pr merge failed: CONFLICTING"})
        merge.assert_called_once()
        self.assertTrue(out.payload.get("review_required"))
        self.assertIn("미머지", out.summary)         # the park reason rides as last_note
        self.assertEqual(out.payload.get("pr_url"), "https://gh/pr/1")

    def test_auto_merge_false_delegate_never_merges(self):
        # auto_merge:false already parks via _merge_decision (review=True). It must
        # NOT call merge_pr — the PR stays open for a human. Fails loudly if it does.
        out, publish, merge = self._run_delegate(
            False, {"merged": True, "note": "should never be called"})
        publish.assert_called_once()
        merge.assert_not_called()
        # review_required was set to True upstream by _merge_decision, not the sink.
        self.assertTrue(out.payload.get("review_required"))


if __name__ == "__main__":
    unittest.main()
