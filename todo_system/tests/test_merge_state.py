"""Spec-table tests for ``todo_system.orchestrator.merge_state`` classifiers.

Pins every row of the documented decision tables with a positive equality on
the returned label, plus named regression guards for the three misreads the
module exists to prevent. The real-git bare-repo fixture tests live next to
the module in ``todo_system/orchestrator/tests/test_merge_state.py``; nothing
here runs git, ``gh`` or the network.
"""

from __future__ import annotations

import subprocess

import pytest

from todo_system.orchestrator import merge_state as ms

_MERGED_AT = "2026-08-19T02:53:13Z"


# ---------------------------------------------------------------------------
# classify_merge_state — "has this landed?"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "is_ancestor_rc, pr, diff_files, expected",
    [
        (1, {"mergedAt": _MERGED_AT, "mergeCommitSha": None}, ["a.py"], ms.MERGED),
        (1, {"mergedAt": None, "mergeCommitSha": "7865e2c"}, ["a.py"], ms.MERGED),
        (0, None, None, ms.MERGED),
        (128, None, None, ms.UNKNOWN),
        (1, {"mergedAt": None, "mergeCommitSha": None}, ["a.py"], ms.UNMERGED),
        (1, None, [], ms.MERGED),
        (1, None, ["a.py"], ms.UNKNOWN),
    ],
    ids=[
        "squash-mergedAt",
        "squash-mergeCommitSha-only",
        "plain-merge-ancestor-rc0",
        "bad-ref-rc128",
        "read-pr-no-merge-record",
        "no-pr-empty-diff",
        "no-pr-nonempty-diff",
    ],
)
def test_classify_merge_state_spec_table(is_ancestor_rc, pr, diff_files, expected):
    assert ms.classify_merge_state(is_ancestor_rc, pr, diff_files) == expected


def test_regression_squash_with_nonzero_ancestor_rc_is_merged():
    # A squash merge writes one brand-new commit, so `--is-ancestor` answers 1
    # ("no") and the two-dot diff still lists the leaf's files — only GitHub's
    # merge record survives. "not an ancestor" must not read as "not merged".
    pr = {"mergedAt": _MERGED_AT, "mergeCommitSha": None}
    assert ms.classify_merge_state(1, pr, ["a.py"]) == ms.MERGED


def test_regression_bad_ref_rc128_is_unknown_not_merged():
    # `--is-ancestor` exits 128 on a ref it cannot resolve. A loose `!= 1`
    # comparison would read that broken invocation as "is an ancestor" = merged.
    # pr/diff are None so only the rc branch decides.
    assert ms.classify_merge_state(128, None, None) == ms.UNKNOWN
    assert ms.classify_merge_state(128, None, None) != ms.MERGED


# ---------------------------------------------------------------------------
# classify_mergeability — "would this merge cleanly?"
# ---------------------------------------------------------------------------

# `UNKNOWN` is one constant shared by both axes: "could not read" is the same
# fact whether the question was "did it land?" or "would it merge?".
@pytest.mark.parametrize(
    "rc, expected",
    [(0, ms.CLEAN), (1, ms.CONFLICTING), (128, ms.UNKNOWN), (None, ms.UNKNOWN)],
    ids=["rc0-clean", "rc1-conflicting", "rc128-unknown", "none-unknown"],
)
def test_classify_mergeability_rc_table(rc, expected):
    assert ms.classify_mergeability(rc) == expected


def test_mergeability_unknown_is_the_shared_merge_state_constant():
    assert ms.classify_mergeability(None) is ms.UNKNOWN
    assert ms.classify_mergeability(128) is ms.UNKNOWN


# ---------------------------------------------------------------------------
# _probe_merge — driven through an injected `_git`, independent of git version
# ---------------------------------------------------------------------------

# git's empty-tree object id: a well-formed OID for the first stdout line.
_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_NOT_CALLED = object()


def _proc(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode,
                                       stdout=stdout, stderr="")


def _fake_git(responses: list) -> tuple:
    """A `_git` stand-in replaying `responses` in call order. Records the git
    args (minus `repo_dir`) and fails loudly on a call nobody scripted."""
    calls: list[tuple] = []

    def fake(repo_dir, *args):
        calls.append(args)
        if len(calls) > len(responses):
            raise AssertionError(f"unexpected git call #{len(calls)}: {args}")
        return responses[len(calls) - 1]

    return fake, calls


@pytest.mark.parametrize(
    "fetch, probe, expected",
    [
        (None, _NOT_CALLED, None),
        (_proc(1), _NOT_CALLED, None),
        (_proc(0), None, None),
        (_proc(0), _proc(1, ""), None),
        (_proc(0), _proc(1, "\n  \n"), None),
        (_proc(0), _proc(1, f"{_TREE_OID}\nCONFLICT (content): a.py\n"), 1),
        (_proc(0), _proc(0, f"{_TREE_OID}\n"), 0),
    ],
    ids=[
        "fetch-could-not-run",
        "fetch-nonzero",
        "probe-could-not-run",
        "bad-ref-rc1-empty-stdout",
        "bad-ref-rc1-whitespace-stdout",
        "real-conflict-rc1-with-oid",
        "clean-rc0-with-oid",
    ],
)
def test_probe_merge_injected(monkeypatch, fetch, probe, expected):
    responses = [fetch] if probe is _NOT_CALLED else [fetch, probe]
    fake, calls = _fake_git(responses)
    monkeypatch.setattr(ms, "_git", fake)

    result = ms._probe_merge("/repo", "feat/x")

    if expected is None:
        assert result is None
    else:
        assert result == expected
    if probe is _NOT_CALLED:
        # Never gauge mergeability against a possibly stale origin/main.
        assert len(calls) == 1
    else:
        assert len(calls) == 2


def test_regression_probe_rc1_empty_stdout_demoted_to_none(monkeypatch):
    # git exits 1 — the "conflicts" code — for an unresolvable ref, with empty
    # stdout. Passed through, a deleted branch would read as a real conflict.
    def no_subprocess(*a, **kw):
        raise AssertionError("real subprocess.run called")

    monkeypatch.setattr(subprocess, "run", no_subprocess)

    fake, _ = _fake_git([_proc(0), _proc(1, "")])
    monkeypatch.setattr(ms, "_git", fake)
    rc = ms._probe_merge("/repo", "gone/branch")
    assert rc is None
    assert ms.classify_mergeability(rc) == ms.UNKNOWN
    assert ms.classify_mergeability(rc) != ms.CONFLICTING

    # Control: a merge that actually ran prints the tree OID first, and its
    # rc 1 must still reach the classifier as a genuine conflict.
    fake, _ = _fake_git([_proc(0), _proc(1, f"{_TREE_OID}\nCONFLICT (content): a.py\n")])
    monkeypatch.setattr(ms, "_git", fake)
    rc = ms._probe_merge("/repo", "feat/x")
    assert rc == 1
    assert ms.classify_mergeability(rc) == ms.CONFLICTING


def test_probe_merge_fetches_with_prune_before_merge_tree(monkeypatch):
    fake, calls = _fake_git([_proc(0), _proc(0, f"{_TREE_OID}\n")])
    monkeypatch.setattr(ms, "_git", fake)

    assert ms._probe_merge("/repo", "feat/x") == 0
    assert calls[0] == ("fetch", "origin", "--prune")
    assert calls[1] == ("merge-tree", "--write-tree", "origin/main", "feat/x")
