"""The vault is one checkout shared by every run, so return-to-main is a race.

`run_next()` calls `_return_vault_to_main()` from a finally block on every exit
path. It used to run `git checkout main` unconditionally, which meant run B
finishing would drag run A's HEAD off `worker/<slug-A>` down to `main` — after
which A's edits and commits landed on `main` at rc=0, silently.

These drive a real git repo rather than mocking subprocess: the thing being
guarded is which ref HEAD actually points at, which mocks cannot show.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import worker


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


class ReturnVaultToMainGuardTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "seed").write_text("x", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "seed")
        # two worker branches: one belonging to a concurrent run, one to us
        _git(self.repo, "branch", "worker/other-slug")
        _git(self.repo, "branch", "worker/mine")
        self._orig_root = worker.ROOT
        worker.ROOT = self.repo

    def tearDown(self):
        worker.ROOT = self._orig_root
        shutil.rmtree(self.repo, ignore_errors=True)

    def _head(self):
        return _git(self.repo, "branch", "--show-current").stdout.strip()

    def _checkout(self, ref):
        result = _git(self.repo, "checkout", "-q", ref)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_foreign_worker_branch_is_left_alone(self):
        """The regression: another run is mid-flight on its own branch."""
        self._checkout("worker/other-slug")

        worker._return_vault_to_main("worker/mine")

        self.assertEqual(self._head(), "worker/other-slug")

    def test_own_worker_branch_returns_to_main(self):
        """Existing behaviour: our own run still hands the vault back on main."""
        self._checkout("worker/mine")

        worker._return_vault_to_main("worker/mine")

        self.assertEqual(self._head(), "main")

    def test_already_on_main_stays_on_main(self):
        self._checkout("main")

        worker._return_vault_to_main(None)

        self.assertEqual(self._head(), "main")

    def test_unknown_own_branch_is_no_op(self):
        """External-repo runs never move vault HEAD, so they may not reclaim it."""
        self._checkout("worker/other-slug")

        worker._return_vault_to_main()

        self.assertEqual(self._head(), "worker/other-slug")

    def test_detached_head_is_left_alone(self):
        self._checkout("main")
        _git(self.repo, "checkout", "-q", "--detach")
        before = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

        worker._return_vault_to_main("worker/mine")

        self.assertEqual(_git(self.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(self._head(), "")

    def test_never_raises_outside_a_repo(self):
        """Contract: called from finally, must never raise on any exit path."""
        non_repo = Path(tempfile.mkdtemp())
        worker.ROOT = non_repo
        try:
            worker._return_vault_to_main("worker/mine")
        finally:
            shutil.rmtree(non_repo, ignore_errors=True)


class RunNextForwardsOwnBranchTest(unittest.TestCase):
    """The guard is only as good as the branch name reaching it."""

    def test_run_next_forwards_own_branch(self):
        def fake_impl(dry_run=False, own_branch_out=None):
            own_branch_out["branch"] = "worker/mine"
            return 0

        with mock.patch.object(worker, "_run_next_impl", side_effect=fake_impl), \
                mock.patch.object(worker, "_return_vault_to_main") as ret:
            self.assertEqual(worker.run_next(dry_run=False), 0)

        ret.assert_called_once_with("worker/mine")

    def test_run_next_forwards_none_when_impl_never_claimed_a_branch(self):
        """External-repo path: nothing recorded, so the guard gets None."""
        with mock.patch.object(worker, "_run_next_impl", return_value=0), \
                mock.patch.object(worker, "_return_vault_to_main") as ret:
            self.assertEqual(worker.run_next(dry_run=False), 0)

        ret.assert_called_once_with(None)

    def test_own_branch_is_forwarded_even_when_impl_raises(self):
        """finally runs on the crash path too — that is where the race bit."""
        def fake_impl(dry_run=False, own_branch_out=None):
            own_branch_out["branch"] = "worker/mine"
            raise RuntimeError("boom")

        with mock.patch.object(worker, "_run_next_impl", side_effect=fake_impl), \
                mock.patch.object(worker, "_return_vault_to_main") as ret:
            with self.assertRaises(RuntimeError):
                worker.run_next(dry_run=False)

        ret.assert_called_once_with("worker/mine")


if __name__ == "__main__":
    unittest.main()
