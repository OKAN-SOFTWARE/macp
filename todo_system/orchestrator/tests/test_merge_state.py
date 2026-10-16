"""Tests for squash-aware merge detection — notably that a *false* `--is-ancestor`
never proves "unmerged", and that an unreadable state stays `unknown` rather than
collapsing into either neighbour."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import merge_state as ms

# Measured 2026-08-19 on `example-org/ai-stock-trading` PR #49 (leaf 26,
# "killswitch partial liquidation"): squash-merged at 02:53:13Z as commit
# `7865e2c`, branch head `ae9f9e5`. Because a squash writes a NEW commit,
# `git merge-base --is-ancestor ae9f9e5 origin/main` exits 1 — the exact signal
# three stages in a row misread as "not merged" before planning a stacked base on
# a branch GitHub had already deleted.
_PR49 = {"mergedAt": "2026-08-19T02:53:13Z", "mergeCommitSha": "7865e2c"}
_PR49_IS_ANCESTOR_RC = 1
# The two-dot diff at that moment held only the FOLLOW-UP leaf's own files
# (+116/-1) — leaf 26's content was already in `main`.
_PR49_DIFF = ["trading_loop.py", "tests/test_observability.py"]

# A PR that was read successfully and carries no merge record at all.
_PR_OPEN = {"mergedAt": None, "mergeCommitSha": None}


class SquashMergeTest(unittest.TestCase):
    def test_measured_pr49_squash_classifies_as_merged(self):
        # The core case: ancestry says "no", the diff is non-empty, and the
        # answer is still `merged`.
        verdict = ms.classify_merge_state(_PR49_IS_ANCESTOR_RC, _PR49, _PR49_DIFF)
        self.assertEqual(verdict, ms.MERGED)
        self.assertNotEqual(verdict, ms.UNMERGED)

    def test_pr_record_outranks_a_non_empty_diff(self):
        # Same fixture read the other way: a follow-up leaf's own files show up
        # in the two-dot diff as a matter of course, so the diff must never veto
        # GitHub's merge record.
        self.assertEqual(
            ms.classify_merge_state(_PR49_IS_ANCESTOR_RC, _PR49,
                                    _PR49_DIFF + ["a.py", "b.py"]),
            ms.MERGED)

    def test_merge_commit_sha_alone_is_enough(self):
        # Either field proves the landing; `gh` has returned each without the
        # other depending on the merge method.
        self.assertEqual(
            ms.classify_merge_state(1, {"mergedAt": None,
                                        "mergeCommitSha": "7865e2c"}, _PR49_DIFF),
            ms.MERGED)


class PlainMergeTest(unittest.TestCase):
    def test_ancestor_exit_zero_is_merged(self):
        # The ordinary (non-squash) merge: the head is still reachable from
        # `origin/main`, and no PR read is needed to say so.
        self.assertEqual(ms.classify_merge_state(0, None, None), ms.MERGED)

    def test_empty_two_dot_diff_is_merged_without_gh(self):
        # `gh` unavailable, ancestry false — but the branch adds nothing `main`
        # lacks, so the content landed however it got there. This is the squash
        # fallback for a repo where `gh` is dead.
        self.assertEqual(ms.classify_merge_state(1, None, []), ms.MERGED)


class UnmergedTest(unittest.TestCase):
    def test_read_pr_with_no_merge_record_is_unmerged(self):
        verdict = ms.classify_merge_state(1, _PR_OPEN, _PR49_DIFF)
        self.assertEqual(verdict, ms.UNMERGED)
        self.assertNotEqual(verdict, ms.MERGED)


class UnknownTest(unittest.TestCase):
    """`unknown` must never be abbreviated: a wrong `merged` opens a stacked PR
    onto a base branch that no longer exists, a wrong `unmerged` re-pushes work
    that already landed."""

    def test_unreadable_gh_with_non_empty_diff_is_unknown(self):
        # GraphQL 503 / dead billing: nothing here distinguishes a follow-up
        # leaf's own files from a genuinely unmerged predecessor.
        verdict = ms.classify_merge_state(1, None, _PR49_DIFF)
        self.assertEqual(verdict, ms.UNKNOWN)
        self.assertNotEqual(verdict, ms.MERGED)
        self.assertNotEqual(verdict, ms.UNMERGED)

    def test_nothing_readable_at_all_is_unknown(self):
        verdict = ms.classify_merge_state(None, None, None)
        self.assertEqual(verdict, ms.UNKNOWN)
        self.assertNotEqual(verdict, ms.MERGED)
        self.assertNotEqual(verdict, ms.UNMERGED)

    def test_pr_not_opened_yet_is_unknown_not_unmerged(self):
        # `gh pr view` exits non-zero when no PR exists, which `_collect` flows
        # as `None` — the same non-committal handling as `stages.py`
        # `gh_pr_merged`. "No PR yet" is an absence of evidence, not evidence.
        verdict = ms.classify_merge_state(1, None, _PR49_DIFF)
        self.assertEqual(verdict, ms.UNKNOWN)
        self.assertNotEqual(verdict, ms.UNMERGED)

    def test_bad_ref_exit_code_is_not_read_as_merged(self):
        # `--is-ancestor` exits 128 on a ref it cannot resolve. A loose `!= 1`
        # comparison would turn that broken invocation into `merged`.
        self.assertNotEqual(ms.classify_merge_state(128, None, _PR49_DIFF),
                            ms.MERGED)


class VerdictSetTest(unittest.TestCase):
    def test_every_verdict_is_one_of_the_three_constants(self):
        allowed = {ms.MERGED, ms.UNMERGED, ms.UNKNOWN}
        cases = [
            (_PR49_IS_ANCESTOR_RC, _PR49, _PR49_DIFF),
            (0, None, None),
            (1, _PR_OPEN, _PR49_DIFF),
            (1, None, []),
            (1, None, _PR49_DIFF),
            (None, None, None),
            (128, {}, []),
            (None, _PR49, None),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertIn(ms.classify_merge_state(*case), allowed)


class PurityTest(unittest.TestCase):
    def test_classifier_never_shells_out(self):
        # Criterion 1 mechanically: the classifier reads already-collected values
        # only. Any subprocess (git, gh) inside it detonates here — which also
        # enforces the hard rule that these tests touch no real repo or network.
        with mock.patch("subprocess.run",
                        side_effect=AssertionError("classify_merge_state shelled out")):
            self.assertEqual(
                ms.classify_merge_state(_PR49_IS_ANCESTOR_RC, _PR49, _PR49_DIFF),
                ms.MERGED)
            self.assertEqual(ms.classify_merge_state(1, None, _PR49_DIFF),
                             ms.UNKNOWN)
            self.assertEqual(ms.classify_merge_state(1, _PR_OPEN, None),
                             ms.UNMERGED)

    def test_classifier_does_not_mutate_its_inputs(self):
        pr = dict(_PR49)
        diff = list(_PR49_DIFF)
        ms.classify_merge_state(_PR49_IS_ANCESTOR_RC, pr, diff)
        self.assertEqual(pr, _PR49)
        self.assertEqual(diff, _PR49_DIFF)


class CollectorTest(unittest.TestCase):
    """The collector unwraps; it never judges. Every subprocess is mocked — these
    tests touch no real repository and no network."""

    @staticmethod
    def _proc(returncode=0, stdout=""):
        return mock.Mock(returncode=returncode, stdout=stdout)

    def test_merge_commit_oid_is_unwrapped_to_merge_commit_sha(self):
        # The live shape `gh pr view --json mergedAt,mergeCommit` returns for a
        # squash-merged PR (measured on PR #49, 2026-08-19). There is no flat
        # `mergeCommitSha` field — requesting one makes `gh` exit 1, and the
        # classifier would then never see the merge record at all.
        raw = {"mergedAt": "2026-08-19T02:53:13Z",
               "mergeCommit": {"oid": "7865e2c9a581fbacd771e93103982abcf3c86fb4"}}
        with mock.patch.object(ms, "_gh_json", return_value=raw):
            pr = ms._pr_record("/repo", "worker/leaf-26")
        self.assertEqual(pr["mergedAt"], "2026-08-19T02:53:13Z")
        self.assertTrue(pr["mergeCommitSha"].startswith("7865e2c"))
        # ...and end to end, that record is what makes the squash read `merged`.
        self.assertEqual(
            ms.classify_merge_state(_PR49_IS_ANCESTOR_RC, pr, _PR49_DIFF),
            ms.MERGED)

    def test_open_pr_has_no_merge_commit(self):
        with mock.patch.object(ms, "_gh_json",
                               return_value={"mergedAt": None, "mergeCommit": None}):
            pr = ms._pr_record("/repo", "worker/leaf-27")
        self.assertIsNone(pr["mergeCommitSha"])
        self.assertEqual(ms.classify_merge_state(1, pr, _PR49_DIFF), ms.UNMERGED)

    def test_failed_fetch_reports_nothing_rather_than_stale_refs(self):
        # Offline / bad remote: every ref below the fetch is of unknown age, and
        # stale refs are half of the misread this module exists for.
        with mock.patch.object(ms, "_git", return_value=self._proc(returncode=1)):
            collected = ms._collect("/repo", "worker/leaf-26")
        self.assertEqual(collected, (None, None, None))
        self.assertEqual(ms.classify_merge_state(*collected), ms.UNKNOWN)

    def test_collect_fetches_with_prune_before_anything_else(self):
        calls = []

        def fake_git(_repo, *args):
            calls.append(args)
            return self._proc(returncode=0, stdout="")

        with mock.patch.object(ms, "_git", side_effect=fake_git), \
                mock.patch.object(ms, "_gh_json", return_value=None):
            ms._collect("/repo", "worker/leaf-26")
        self.assertEqual(calls[0], ("fetch", "origin", "--prune"))

    def test_collect_returns_raw_values_not_a_verdict(self):
        with mock.patch.object(ms, "_git",
                               side_effect=[self._proc(0), self._proc(1),
                                            self._proc(0, "trading_loop.py\n")]), \
                mock.patch.object(ms, "_gh_json", return_value=None):
            rc, pr, diff_files = ms._collect("/repo", "worker/leaf-26")
        self.assertEqual(rc, 1)          # the exit code itself, not a bool
        self.assertIsNone(pr)
        self.assertEqual(diff_files, ["trading_loop.py"])


class MergeabilityMappingTest(unittest.TestCase):
    """The other axis: "would it merge?", never "did it land?".

    `git merge-tree --write-tree` documents exactly three buckets — "for a
    successful, non-conflicted merge, the exit status is 0", "when the merge has
    conflicts, the exit status is 1", and "if the merge is not able to complete
    (or start) due to some kind of error, the exit status is something other than
    0 or 1" (git-merge-tree(1), read on git 2.44.0). The third bucket is a
    failure to *measure* and must never be laundered into `conflicting`.
    """

    def test_rc_mapping_table(self):
        table = [
            (0, ms.CLEAN),
            (1, ms.CONFLICTING),
            (2, ms.UNKNOWN),      # "something other than 0 or 1"
            (128, ms.UNKNOWN),    # git's usual code for a ref it cannot resolve
            (129, ms.UNKNOWN),    # usage error — a bug in the caller, not a merge
            (None, ms.UNKNOWN),   # the probe could not be run at all
        ]
        for rc, expected in table:
            with self.subTest(rc=rc):
                self.assertEqual(ms.classify_mergeability(rc), expected)

    def test_non_zero_is_not_blanket_conflicting(self):
        # The reason `rc == 1` is compared strictly, and the same trap
        # `classify_merge_state` avoids on `is_ancestor_rc`: a loose `!= 0` would
        # report a misspelled branch name as a genuine merge conflict, which is
        # the one misreading that would send a planner down a real rework path.
        for rc in (2, 3, 128, 129, None):
            with self.subTest(rc=rc):
                self.assertNotEqual(ms.classify_mergeability(rc), ms.CONFLICTING)
                self.assertNotEqual(ms.classify_mergeability(rc), ms.CLEAN)

    def test_unreadable_reuses_the_shared_unknown_constant(self):
        # `UNKNOWN` is shared with the merge-state axis on purpose — a second
        # spelling of "could not read" would invite a caller to treat one of them
        # as informative.
        self.assertEqual(ms.classify_mergeability(None), ms.UNKNOWN)
        self.assertIs(ms.classify_mergeability(None), ms.UNKNOWN)

    def test_every_verdict_is_one_of_the_three_constants(self):
        allowed = {ms.CLEAN, ms.CONFLICTING, ms.UNKNOWN}
        for rc in (0, 1, 2, 3, 127, 128, 129, 255, None):
            with self.subTest(rc=rc):
                self.assertIn(ms.classify_mergeability(rc), allowed)

    def test_classifier_never_shells_out(self):
        # Same mechanical purity check as `PurityTest`: the verdict is formed
        # from an already-collected exit code, so any subprocess here detonates.
        with mock.patch(
                "subprocess.run",
                side_effect=AssertionError("classify_mergeability shelled out")):
            self.assertEqual(ms.classify_mergeability(0), ms.CLEAN)
            self.assertEqual(ms.classify_mergeability(1), ms.CONFLICTING)
            self.assertEqual(ms.classify_mergeability(128), ms.UNKNOWN)


def _run_git(cwd, *args):
    """Drive the fixture repos directly rather than through `ms._git`, so a bug in
    the module under test cannot quietly build a broken fixture and pass."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)


class ProbeFixtureTest(unittest.TestCase):
    """A real bare origin + clone under `tempfile.mkdtemp()`. No network: `origin`
    is a local path, so `fetch origin --prune` succeeds offline.

    The fixture's one difficulty is the conflicting case. Cutting a branch and
    leaving `main` where it was makes the merge-base the branch point itself, and
    *everything* merges clean — the test would pass while measuring nothing. So
    `main` is advanced on f.txt's single line only AFTER both branches are cut,
    and `test_same_line_divergence_is_conflicting` carries a positive control
    against exactly that failure.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.repo = self.root / "clone"
        self._git_ok(self.root, "init", "--bare", "-q", "-b", "main", "origin.git")
        self._git_ok(self.root, "clone", "-q", str(self.root / "origin.git"),
                     "clone")
        self._git_ok(self.repo, "config", "user.email", "t@t")
        self._git_ok(self.repo, "config", "user.name", "t")
        (self.repo / "f.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        self._git_ok(self.repo, "add", "-A")
        self._git_ok(self.repo, "commit", "-qm", "seed")
        self._git_ok(self.repo, "push", "-q", "origin", "main")
        # Both branches are cut from the seed, BEFORE `main` moves.
        self._git_ok(self.repo, "branch", "feat/clean")
        self._git_ok(self.repo, "branch", "feat/conflict")
        self._commit_on("feat/clean", "other.txt", "clean side\n")
        self._commit_on("feat/conflict", "f.txt", "branch side\n")
        # ...and only now does the trunk move, on f.txt's one and only line.
        self._commit_on("main", "f.txt", "main side\n")
        self._git_ok(self.repo, "push", "-q", "origin", "main")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _git_ok(self, cwd, *args):
        result = _run_git(cwd, *args)
        self.assertEqual(result.returncode, 0,
                         f"fixture step failed: git {' '.join(args)}\n{result.stderr}")
        return result

    def _commit_on(self, branch, path, text):
        self._git_ok(self.repo, "checkout", "-q", branch)
        (self.repo / path).write_text(text, encoding="utf-8")
        self._git_ok(self.repo, "add", "-A")
        self._git_ok(self.repo, "commit", "-qm", f"{branch}: {path}")

    def _porcelain(self):
        return _run_git(self.repo, "status", "--porcelain").stdout

    def _head(self):
        return _run_git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def _current_branch(self):
        return _run_git(self.repo, "branch", "--show-current").stdout.strip()

    def test_branch_touching_only_other_files_is_clean(self):
        rc = ms._probe_merge(str(self.repo), "feat/clean")
        self.assertEqual(rc, 0)
        self.assertEqual(ms.classify_mergeability(rc), ms.CLEAN)

    def test_same_line_divergence_is_conflicting(self):
        rc = ms._probe_merge(str(self.repo), "feat/conflict")
        verdict = ms.classify_mergeability(rc)
        self.assertEqual(rc, 1)
        self.assertEqual(verdict, ms.CONFLICTING)
        # Positive control against a vacuous fixture: had `main` not advanced on
        # f.txt's line after the branch was cut, this would merge clean and the
        # assertion above would be testing nothing at all.
        self.assertNotEqual(verdict, ms.CLEAN)

    def test_clean_and_conflicting_are_actually_distinguished(self):
        # The pair, side by side: the fixture separates the two verdicts rather
        # than returning one of them for everything.
        self.assertNotEqual(
            ms.classify_mergeability(ms._probe_merge(str(self.repo), "feat/clean")),
            ms.classify_mergeability(ms._probe_merge(str(self.repo), "feat/conflict")))

    def test_nonexistent_branch_is_unknown_not_conflicting(self):
        # The regression this pair of asserts exists for. git's man page promises
        # that a merge which cannot start exits "something other than 0 or 1",
        # but git 2.44.0 exits **1** for an unresolvable ref — indistinguishable
        # by exit code alone from a real conflict (measured 2026-08-19; stderr
        # reads `merge-tree: feat/does-not-exist - not something we can merge`).
        # `_probe_merge` must therefore report it as unmeasured, or a deleted
        # branch would be announced as a conflict to resolve.
        rc = ms._probe_merge(str(self.repo), "feat/does-not-exist")
        self.assertIsNone(rc)
        verdict = ms.classify_mergeability(rc)
        self.assertEqual(verdict, ms.UNKNOWN)
        self.assertNotEqual(verdict, ms.CONFLICTING)

    def test_raw_git_really_does_exit_1_on_a_bad_ref(self):
        # Pins the surprising measurement itself, straight from git and bypassing
        # `_probe_merge` entirely. If a future git ever honours its documentation
        # and returns 128 here, this fails loudly and the unwrapping above can be
        # revisited — rather than silently becoming dead defensive code.
        raw = _run_git(self.repo, "merge-tree", "--write-tree", "origin/main",
                       "feat/does-not-exist")
        self.assertEqual(raw.returncode, 1)
        self.assertEqual(raw.stdout.strip(), "")
        self.assertIn("not something we can merge", raw.stderr)

    def test_a_real_conflict_still_prints_a_tree_oid(self):
        # The other half of the discriminator: an actual conflict exits 1 *with*
        # stdout, which is what keeps the empty-stdout rule from swallowing it.
        raw = _run_git(self.repo, "merge-tree", "--write-tree", "origin/main",
                       "feat/conflict")
        self.assertEqual(raw.returncode, 1)
        self.assertNotEqual(raw.stdout.strip(), "")

    def test_probe_leaves_worktree_index_and_head_untouched(self):
        # Dirty the tree first, deliberately. An already-clean checkout staying
        # clean would also pass for a probe that destroyed everything, so the
        # control carries uncommitted and untracked state that any ref-moving or
        # file-rewriting git command would visibly eat.
        (self.repo / "f.txt").write_text("uncommitted edit\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("keep me\n", encoding="utf-8")
        before_status = self._porcelain()
        before_head = self._head()
        before_branch = self._current_branch()
        self.assertNotEqual(before_status.strip(), "",
                            "the control is only meaningful on a dirty tree")

        for branch in ("feat/clean", "feat/conflict", "feat/does-not-exist"):
            with self.subTest(branch=branch):
                ms._probe_merge(str(self.repo), branch)
                self.assertEqual(self._porcelain(), before_status)
                self.assertEqual(self._head(), before_head)
                self.assertEqual(self._current_branch(), before_branch)
        # ...and the bytes on disk, which `status --porcelain` alone would not
        # catch if a file were rewritten and left staged-looking.
        self.assertEqual((self.repo / "f.txt").read_text(encoding="utf-8"),
                         "uncommitted edit\n")
        self.assertEqual((self.repo / "untracked.txt").read_text(encoding="utf-8"),
                         "keep me\n")

    def test_failed_fetch_reports_unknown_instead_of_a_stale_reading(self):
        # The stale-ref rule, on a real repo: point `origin` at nothing while the
        # local `origin/main` stays perfectly readable. A probe that skipped the
        # fetch would happily return `clean` here off the stale ref — which is
        # the exact class of misread this module exists to prevent.
        self.assertEqual(
            ms.classify_mergeability(ms._probe_merge(str(self.repo), "feat/clean")),
            ms.CLEAN)
        self._git_ok(self.repo, "remote", "set-url", "origin",
                     str(self.root / "does-not-exist.git"))
        rc = ms._probe_merge(str(self.repo), "feat/clean")
        self.assertIsNone(rc)
        self.assertEqual(ms.classify_mergeability(rc), ms.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
