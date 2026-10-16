"""Tell a predecessor leaf that already landed from one that is genuinely open.

A stacked PR's base is only valid while the leaf underneath it is still unmerged,
and both observations a planner reaches for lie by default. `git branch -r
--contains` reads local tracking refs, so without `fetch --prune` a remote branch
deleted on merge still looks alive. Worse, a **squash** merge writes one brand-new
commit, so `git merge-base --is-ancestor <leaf-head> origin/main` answers *false*
about work that is already present in `main` in full. Hence the single rule this
module exists to encode: **"not an ancestor" != "not merged"**. Measured
2026-08-19 on `example-org/ai-stock-trading` PR #49, squash-merged four minutes
before a run that then planned a stacked base on top of it.

The verdict is a *label* for a planner to route on; it never decides pass/fail
(see `stages.py` `overall_status`) and must not be used to excuse one. It answers
"has this landed?" and deliberately NOT "would it merge cleanly?" — a clean
two-dot diff says nothing about a 3-way merge, whose merge-base the squash has
already moved backwards (PR #50 read `CONFLICTING` while its two-dot diff was
exactly our two files). Mergeability is measured separately, by
`classify_mergeability`/`_probe_merge` below (`git merge-tree --write-tree`).

Pure and offline: `classify_merge_state` reads already-collected values only. The
thin `python -m todo_system.orchestrator.merge_state <repo_dir> <branch>` entry
point is the only thing here that touches git or `gh`.
"""
from __future__ import annotations

import json
import subprocess
import sys

MERGED = "merged"
UNMERGED = "unmerged"
UNKNOWN = "unknown"

# The second, independent axis: "would this merge cleanly?" as opposed to "has
# this landed?". Deliberately shares `UNKNOWN` rather than minting a second one —
# a failure to read is the same fact on either axis, and two spellings of it
# would invite a caller to treat one as informative.
CLEAN = "clean"
CONFLICTING = "conflicting"

# Judged against the remote trunk, never a local `main` that may sit behind it —
# reading a local ref and calling it the truth is the stale half of this misread.
_BASE = "origin/main"


def classify_merge_state(is_ancestor_rc: int | None, pr: dict | None,
                         diff_files: list[str] | None) -> str:
    """Has `branch` already landed in `origin/main`? `None` = "could not read".

    `is_ancestor_rc` is the exit code of `git merge-base --is-ancestor <branch>
    origin/main`; `pr` the parsed `gh pr view --json mergedAt,mergeCommitSha`
    (`None` = unreadable *or* no PR opened yet); `diff_files` the file list from
    `git diff --name-only origin/main..<branch>` (`None` = unread, `[]` = read
    and empty). Answers only "did this land?", never "would it merge cleanly?".
    """
    # GitHub's own record outranks every local inference, and it is the only
    # input that survives a squash: the merge writes a new commit, so ancestry
    # and diff both go quiet while `mergedAt`/`mergeCommitSha` stay set.
    if isinstance(pr, dict) and (pr.get("mergedAt") or pr.get("mergeCommitSha")):
        return MERGED
    # A plain (non-squash) merge leaves the branch head reachable. Compared
    # strictly against 0 because `--is-ancestor` uses 1 for "no" but other codes
    # for "bad ref"; a loose `!= 1` would read a broken invocation as merged.
    if is_ancestor_rc == 0:
        return MERGED
    # The PR was read and carries no merge record — authoritative negative
    # evidence, and the only thing that separates `unmerged` from `unknown`.
    if isinstance(pr, dict):
        return UNMERGED
    # No PR record to consult (`gh` down, or none opened), but the two-dot diff
    # came back empty: the branch adds nothing `main` lacks, so its content has
    # landed however it got there. Used ASYMMETRICALLY on purpose — a non-empty
    # diff proves nothing, since a follow-up leaf's own files belong there.
    if diff_files == []:
        return MERGED
    # Nothing dependable left. Never fold this into a neighbour: a wrong `merged`
    # plans a stacked PR onto a base branch that no longer exists, and a wrong
    # `unmerged` re-pushes work that already landed.
    return UNKNOWN


def classify_mergeability(rc: int | None) -> str:
    """Would `branch` merge cleanly into `origin/main`? `None` = "could not read".

    `rc` is the exit code of `git merge-tree --write-tree origin/main <branch>`,
    which documents exactly three buckets: 0 for "a successful, non-conflicted
    merge", 1 for "the merge has conflicts", and "something other than 0 or 1"
    when the merge "is not able to complete (or start) due to some kind of error"
    (git-merge-tree(1), read on git 2.44.0). That third bucket is a failure to
    *measure*, not a conflict, so it reuses `UNKNOWN`: a loose `!= 0` would report
    a misspelled ref (128) or a usage error (129) as a genuine conflict, the same
    trap `classify_merge_state` sidesteps by comparing `is_ancestor_rc` strictly
    against 0. Note that git does not actually honour that third bucket for a
    bad ref (it exits 1); keeping this mapping honest is `_probe_merge`'s job,
    which hands such a run on as `None` rather than as a fake conflict.

    Like `classify_merge_state` this is a *label* for a planner to route on; it
    never decides pass/fail (see `stages.py` `overall_status`) and must not be
    used to excuse one. Answers only "would it merge?", never "did it land?".
    """
    if rc == 0:
        return CLEAN
    if rc == 1:
        return CONFLICTING
    return UNKNOWN


def _git(repo_dir: str, *args: str) -> subprocess.CompletedProcess | None:
    """Run one git command in `repo_dir`; `None` if it could not be run at all."""
    try:
        return subprocess.run(["git", "-C", repo_dir, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None


def _gh_json(repo_dir: str, *args: str):
    """Run one `gh` command expected to print JSON; `None` on any failure.

    `cwd` matters: `gh pr view <branch>` resolves the repository from the working
    directory, not from git's `-C`. A missing PR exits non-zero and so flows as
    `None` too — the same non-committal handling as `stages.py` `gh_pr_merged`.
    """
    try:
        r = subprocess.run(["gh", *args], cwd=repo_dir, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _collect(repo_dir: str, branch: str) -> tuple:
    """Gather the three raw observations for `branch`. Collect and unwrap only —
    no verdict is decided here, and anything unreadable is passed on as `None`."""
    # Unconditional and FIRST: stale tracking refs are half of this bug. If the
    # fetch itself fails, every ref below is of unknown age, so nothing is
    # reported rather than something stale.
    fetched = _git(repo_dir, "fetch", "origin", "--prune")
    if fetched is None or fetched.returncode != 0:
        return None, None, None
    anc = _git(repo_dir, "merge-base", "--is-ancestor", branch, _BASE)
    diff = _git(repo_dir, "diff", "--name-only", f"{_BASE}..{branch}")
    diff_files = ([ln for ln in diff.stdout.splitlines() if ln.strip()]
                  if diff is not None and diff.returncode == 0 else None)
    return (anc.returncode if anc is not None else None,
            _pr_record(repo_dir, branch),
            diff_files)


def _pr_record(repo_dir: str, branch: str) -> dict | None:
    """Read `branch`'s PR as the flat `{mergedAt, mergeCommitSha}` the classifier
    documents. `None` = unreadable or no PR.

    `gh pr view` has **no** `mergeCommitSha` field: the sha arrives nested as
    `mergeCommit.oid` (verified 2026-08-19 against PR #49, oid `7865e2c9…`).
    Asking for the flat name makes `gh` exit 1, which would quietly disable the
    one input that survives a squash and strand every real call on the diff
    fallback. Unwrapping only — no verdict is formed here.
    """
    pr = _gh_json(repo_dir, "pr", "view", branch, "--json",
                  "mergedAt,mergeCommit")
    if not isinstance(pr, dict):
        return None
    commit = pr.get("mergeCommit")
    return {"mergedAt": pr.get("mergedAt"),
            "mergeCommitSha": commit.get("oid") if isinstance(commit, dict) else None}


def _probe_merge(repo_dir: str, branch: str) -> int | None:
    """Trial-merge `branch` into `origin/main` and return the raw exit code.

    Collect only — the verdict is `classify_mergeability`'s, the same split
    `_collect` keeps from `classify_merge_state`. `None` = the probe could not be
    run, and a failed fetch is one such case: gauging mergeability against a
    stale `origin/main` would reproduce the exact misread this module exists to
    prevent, so the `_collect` rule holds here too — fetch first, and report
    nothing rather than something stale. (The CLI therefore fetches twice; that
    is deliberate, since `_collect`'s contract is frozen.)

    Read-only in the sense that matters: `--write-tree` does deposit tree objects
    in `.git/objects`, but the working tree, the index and HEAD are left bit-for-
    bit alone, and the trial merge is the only git write of any kind — `repo_dir`
    may be an external clone whose working tree is shared with a concurrent run.
    """
    fetched = _git(repo_dir, "fetch", "origin", "--prune")
    if fetched is None or fetched.returncode != 0:
        return None
    probe = _git(repo_dir, "merge-tree", "--write-tree", _BASE, branch)
    if probe is None:
        return None
    # git contradicts its own man page here, so the raw code cannot be passed on
    # unexamined. The page promises that a merge which "is not able to complete
    # (or start)" exits "something other than 0 or 1", but an unresolvable ref
    # exits **1** — precisely the code that means "conflicts" (measured on git
    # 2.44.0, 2026-08-19: rc 1, empty stdout, and `merge-tree: <ref> - not
    # something we can merge` on stderr). Passed through, a deleted or misspelled
    # branch would be reported as a genuine conflict: a planner would be sent to
    # resolve a merge that never happened.
    #
    # Discriminated on structure, not on that English sentence, which is exactly
    # the kind of string that shifts between versions and locales: a merge that
    # really ran always prints the new tree's OID as its first stdout line (the
    # documented `NEWTREE=$(git merge-tree --write-tree ...)` usage), and a
    # conflicted one prints that OID plus the conflict report. Empty stdout on a
    # non-zero exit therefore means the merge never started — unmeasured, not
    # conflicted, so it flows on as `None` like every other unreadable input.
    if probe.returncode != 0 and not probe.stdout.strip():
        return None
    return probe.returncode


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python -m todo_system.orchestrator.merge_state "
              "<repo_dir> <branch>", file=sys.stderr)
        raise SystemExit(2)
    # First line stays one bare merge-state word: callers that read only `head -1`
    # predate mergeability and must not start seeing a second axis. New output
    # appends, never reorders.
    print(classify_merge_state(*_collect(sys.argv[1], sys.argv[2])))
    print("mergeable="
          f"{classify_mergeability(_probe_merge(sys.argv[1], sys.argv[2]))}")
