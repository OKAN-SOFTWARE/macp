"""`reclaim_stale_worktrees` removes merged, clean `wt-*` worktrees — and only those.

The active counterpart to `stale_worktree_actions`. Detection (conds 1–2) is
shared via `_removable_candidates` and already pinned by
`test_worker_stale_worktree.py`; this file pins the two *removal-only* gates and
the removal itself, against a real git repo with a **bare origin** so branches
can carry upstreams and `origin/main` is the base ref (the one the production
sweep judges against).

Three failure modes these tests exist to stop.

**The vacuous no-op.** Worker PRs land with `gh pr merge --squash`, so the
`worker/<slug>` branch is never pushed — it has no upstream. A cond-3 check
written as a bare `git rev-list @{u}..` (or `--not --remotes`) treats every such
branch as "has unpushed work" and preserves all of them, i.e. reclaims nothing
in production while a real-merge test goes green.
`test_squash_merge_no_upstream_worktree_is_reclaimed` is the control that fails
on that no-op.

**The over-eager removal.** dirty / unpushed / in-use worktrees may hold the
only copy of work; each must survive. And a preservation test is only honest if
the branch would *otherwise* be reclaimed — so `test_unpushed_...` first proves
via `_removable_candidates` that cond 1 holds, making cond 3 the sole gate under
test, and the dirty / in-use tests plant a clean positive control that IS
reclaimed alongside the one that must not be.

**The stalled tick.** Removal runs on the 5-minute maintenance tick, so a broken
git must remove nothing and return `[]`, never raise.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import worker
from todo_system.tests.test_worker_stale_worktree import (
    _HAS_WRITE_TREE,
    _TIMEOUT,
    _Fixture,
)


class _ReclaimFixture(_Fixture):
    """`_Fixture` plus a bare origin, so branches can carry upstreams.

    Cond 3 (no unpushed work) only means something when `@{u}` can resolve,
    which needs a real remote; and `_resolve_base_ref` prefers `origin/main`,
    the ref the production sweep actually judges "merged" against. The
    "…into_origin" builders push *main* (never the worker branch), so the branch
    stays upstream-less — mirroring a real worker branch that only ever reached
    origin as the squash-merged PR.
    """

    def __init__(self, tmp: Path) -> None:
        super().__init__(tmp)
        self.origin = tmp / "origin.git"
        proc = subprocess.run(
            ["git", "init", "--bare", "-q", str(self.origin)],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        assert proc.returncode == 0, f"bare init failed: {proc.stderr}"
        self.git("remote", "add", "origin", str(self.origin))
        self._push_main()

    def _push_main(self) -> None:
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin")

    def reclaim(self) -> list[str]:
        return worker.reclaim_stale_worktrees(
            repo_root=self.repo, worktree_parent=self.worker_root
        )

    def branch_names(self) -> list[str]:
        return self.git("branch", "--format=%(refname:short)").stdout.split()

    def worktree_names(self) -> list[str]:
        listing = self.git("worktree", "list", "--porcelain").stdout
        return [
            Path(r["worktree"]).name
            for r in worker._parse_worktree_porcelain(listing)
            if r.get("worktree")
        ]

    def is_candidate(self, slug: str) -> bool:
        """Read-only premise check: does the shared detection see `wt-<slug>`?"""
        cands, _, _ = worker._removable_candidates(self.repo, self.worker_root.resolve())
        return f"wt-{slug}" in [Path(p).name for p, _branch in cands]

    # --- candidate builders (all leave the repo on `main`) ---

    def merged_into_origin(self, slug: str) -> Path:
        """Real-merge a branch into main, land it on origin, keep the worktree.

        Ancestor of origin/main → cond 1 signal A. Never pushed → no upstream →
        cond 3 passes via its no-upstream arm.
        """
        branch = self.merged_branch(slug)   # advances local main (--no-ff)
        self._push_main()                    # origin/main now contains the merge
        return self.add_worktree(slug, branch)

    def squashed_into_origin(self, slug: str) -> Path:
        """Squash a branch's content onto main, land on origin, keep worktree.

        Branch tip is NOT an ancestor (cond 1 rides signal B, merge-tree) and it
        has no upstream, so a cond 3 using a bare `@{u}..` would wrongly hold it.
        """
        branch = self.squashed_branch(slug)
        self._push_main()
        return self.add_worktree(slug, branch)

    def unpushed_over_upstream(self, slug: str) -> Path:
        """Contained in origin/main AND tracked, but with one unpushed commit.

        Built so cond 1 PASSES while cond 3 FAILS: the branch adds a file, is
        pushed (upstream set at that commit), then removes the file in a second,
        unpushed commit — so its tip tree equals origin/main's (merge-tree →
        contained) yet `@{u}..HEAD` counts 1. A vacuous unpushed test — one where
        cond 1 already holds the branch back — would stay green even if cond 3
        were deleted; this makes cond 3 the sole gate under test.
        """
        branch = f"worker/{slug}"
        self.git("checkout", "-q", "-b", branch)
        self.commit(f"{slug}-temp.txt", "temp\n", f"add temp {slug}")
        self.git("push", "-q", "-u", "origin", branch)   # upstream at 'add temp'
        self.git("rm", "-q", f"{slug}-temp.txt")
        self.git("commit", "-q", "-m", f"remove temp {slug} (unpushed)")
        self.git("checkout", "-q", "main")
        self.git("fetch", "-q", "origin")
        return self.add_worktree(slug, branch)

    def unmerged_into_origin(self, slug: str) -> Path:
        """A branch whose work never reached origin/main — cond 1 must FAIL.

        Mirrors wt-macp-orphan-superseded on the live host: a superseded branch
        carrying real commits that were never merged, so `_is_contained_in_base`
        is False and `_removable_candidates` drops it before the gate loop. Clean
        (cond 2 holds), so the drop is 'unmerged', not 'dirty'. Never pushed, so
        it also has no upstream — but cond 1 excludes it first, so cond 3 never
        runs. Built on `origin/main` as it stands, so a preceding
        `merged_into_origin` control leaves it genuinely uncontained.
        """
        branch = self.unmerged_branch(slug)   # commit on branch, repo back on main
        return self.add_worktree(slug, branch)


class ReclaimRemovalTest(unittest.TestCase):
    """Positive controls: all four conditions hold, so the worktree + branch go."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = _ReclaimFixture(Path(self._tmp.name))

    def test_real_merge_worktree_is_reclaimed(self):
        path = self.fx.merged_into_origin("done-task")
        reported = self.fx.reported_path("done-task")

        actions = self.fx.reclaim()

        self.assertEqual(actions, [f"정리 완료: {reported} (worktree+branch 제거)"])
        self.assertFalse(path.is_dir(), "worktree dir survived removal")
        self.assertNotIn("worker/done-task", self.fx.branch_names())
        self.assertNotIn("wt-done-task", self.fx.worktree_names())

    @unittest.skipUnless(_HAS_WRITE_TREE, "git has no merge-tree --write-tree")
    def test_squash_merge_no_upstream_worktree_is_reclaimed(self):
        """Anti-vacuous control: cond 1 via merge-tree, cond 3 via no-upstream.

        Fails if cond 3 re-preserves squashes (a bare `@{u}..`) — the production
        no-op — or if signal B is dropped from `_is_contained_in_base`.
        """
        path = self.fx.squashed_into_origin("squashed-task")
        reported = self.fx.reported_path("squashed-task")

        # Premise: not an ancestor of origin/main, so this exercises signal B +
        # the no-upstream arm, not a real-merge shortcut.
        ancestor = subprocess.run(
            ["git", "-C", str(self.fx.repo), "merge-base", "--is-ancestor",
             "worker/squashed-task", "origin/main"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        self.assertNotEqual(ancestor.returncode, 0, "premise broken: branch is an ancestor")

        actions = self.fx.reclaim()

        self.assertEqual(actions, [f"정리 완료: {reported} (worktree+branch 제거)"])
        self.assertFalse(path.is_dir(), "squash-merged worktree survived removal")
        self.assertNotIn("worker/squashed-task", self.fx.branch_names())
        self.assertNotIn("wt-squashed-task", self.fx.worktree_names())


class ReclaimPreservationTest(unittest.TestCase):
    """Any single failed gate leaves the worktree AND its branch untouched."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = _ReclaimFixture(Path(self._tmp.name))

    def test_dirty_worktree_is_preserved(self):
        """Uncommitted work outranks a merged branch — it may be the only copy."""
        clean = self.fx.merged_into_origin("clean-task")
        dirty = self.fx.merged_into_origin("dirty-task")
        (dirty / "scratch.txt").write_text("unsaved\n", encoding="utf-8")
        dirty_reported = self.fx.reported_path("dirty-task")

        actions = self.fx.reclaim()

        # clean-task is the positive control: it proves the sweep is live, so
        # dirty surviving is cond 2 holding, not an inert fixture.
        self.assertFalse(clean.is_dir(), "clean control was not reclaimed")
        self.assertNotIn("worker/clean-task", self.fx.branch_names())
        self.assertTrue(dirty.is_dir(), "dirty worktree was removed")
        self.assertIn("worker/dirty-task", self.fx.branch_names())
        self.assertIn("wt-dirty-task", self.fx.worktree_names())
        # No longer silent: the cond 2 drop is logged, and that 정리 제외 is the
        # *only* line naming dirty-task — never a 정리 완료/보류 (removal).
        self.assertEqual(
            [a for a in actions if "dirty-task" in a],
            [f"정리 제외: {dirty_reported} (dirty)"],
        )

    def test_unpushed_commit_worktree_is_preserved(self):
        path = self.fx.unpushed_over_upstream("wip-task")

        # Premise: cond 1 really holds (branch content is in origin/main), so the
        # hold below is cond 3, not the branch being unmerged.
        self.assertTrue(
            self.fx.is_candidate("wip-task"),
            "premise broken: unpushed branch is not even a detection candidate",
        )

        actions = self.fx.reclaim()

        self.assertTrue(path.is_dir(), "unpushed worktree was removed")
        self.assertIn("worker/wip-task", self.fx.branch_names())
        self.assertIn("wt-wip-task", self.fx.worktree_names())
        self.assertTrue(
            any("unpushed" in a for a in actions),
            f"expected an unpushed hold line, got {actions}",
        )

    def test_in_use_worktree_is_preserved(self):
        clean = self.fx.merged_into_origin("clean-task")
        busy = self.fx.merged_into_origin("busy-task")

        def fake_pgrep(*args: str) -> str:
            # `_pgrep("-f", <path>)`: a live match (nonempty) only for busy;
            # the rc-1 "no match" shape (empty string) for everything else.
            target = args[-1] if args else ""
            return "424242 claude -p ...\n" if "busy-task" in target else ""

        with mock.patch.object(worker, "_pgrep", side_effect=fake_pgrep):
            actions = self.fx.reclaim()

        self.assertFalse(clean.is_dir(), "clean control was not reclaimed")
        self.assertNotIn("worker/clean-task", self.fx.branch_names())
        self.assertTrue(busy.is_dir(), "in-use worktree was removed")
        self.assertIn("worker/busy-task", self.fx.branch_names())
        self.assertIn("wt-busy-task", self.fx.worktree_names())
        self.assertTrue(
            any("in-use" in a for a in actions),
            f"expected an in-use hold line, got {actions}",
        )

    def test_unmerged_worktree_is_excluded_with_observable_reason(self):
        """Superseded/unmerged branch survives — and no longer silently.

        The live regression this run diagnosed: wt-macp-orphan-superseded sat in
        the worker root, clean and in scope, but its branch was never merged into
        origin/main (cond 1 False), so `_removable_candidates` dropped it with no
        log line at all — indistinguishable from "nothing to clean". It must
        survive AND announce why, via `정리 제외: … (unmerged)`.
        """
        clean = self.fx.merged_into_origin("clean-task")
        unmerged = self.fx.unmerged_into_origin("superseded-task")
        unmerged_reported = self.fx.reported_path("superseded-task")

        # Premise: cond 1 genuinely fails (branch not contained in origin/main),
        # and it never even becomes a detection candidate — so the survival below
        # is the cond 1 drop, and the reason is 'unmerged' (clean), not 'dirty'.
        self.assertFalse(
            self.fx.is_candidate("superseded-task"),
            "premise broken: unmerged branch became a detection candidate",
        )
        ancestor = subprocess.run(
            ["git", "-C", str(self.fx.repo), "merge-base", "--is-ancestor",
             "worker/superseded-task", "origin/main"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        self.assertNotEqual(ancestor.returncode, 0, "premise broken: branch is an ancestor")

        actions = self.fx.reclaim()

        # clean control proves the sweep is live (not an inert fixture that finds
        # nothing), so the superseded survival is cond 1 holding, not inaction.
        self.assertFalse(clean.is_dir(), "clean control was not reclaimed")
        self.assertNotIn("worker/clean-task", self.fx.branch_names())
        # the superseded worktree + branch survive untouched …
        self.assertTrue(unmerged.is_dir(), "unmerged worktree was removed")
        self.assertIn("worker/superseded-task", self.fx.branch_names())
        self.assertIn("wt-superseded-task", self.fx.worktree_names())
        # … and its exclusion is observable now, not a silent []. Anti-silent
        # control: the *only* line naming it is that 정리 제외 — never a removal.
        self.assertEqual(
            [a for a in actions if "superseded-task" in a],
            [f"정리 제외: {unmerged_reported} (unmerged)"],
        )


class ReclaimFailOpenTest(unittest.TestCase):
    """The 5-minute tick must survive a broken git: remove nothing, return []."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_timeout_removes_nothing(self):
        fx = _ReclaimFixture(self.tmp)
        path = fx.merged_into_origin("clean-task")

        # Control: a healthy scan really finds this candidate (read-only — it
        # does not remove), so the [] below is the timeout being swallowed, not
        # an empty fixture.
        self.assertTrue(fx.is_candidate("clean-task"))

        with mock.patch(
            "todo_system.worker.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1),
        ):
            self.assertEqual(fx.reclaim(), [])

        self.assertTrue(path.is_dir(), "timeout removed a worktree")
        self.assertIn("worker/clean-task", fx.branch_names())
        self.assertIn("wt-clean-task", fx.worktree_names())

    def test_non_repo_returns_empty(self):
        self.assertEqual(
            worker.reclaim_stale_worktrees(
                repo_root=self.tmp / "nope", worktree_parent=self.tmp / "worker"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
