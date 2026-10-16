"""Tell a *red* GitHub Actions run apart: dead billing, missing runner, real bug.

The three reds look identical in `gh run list` but have opposite prescriptions —
`budget_blocked` only a human's payment clears, `runner_unavailable` is a fixable
config defect (bad label, self-hosted box down), `real_failure` is the diff's own
problem. Until now that knowledge lived only as prose in the stage prompts, so
every agent re-derived it by hand. The verdict here is a *label* on a red run; it
never decides pass/fail (see `stages.py` `overall_status`) and must not be used to
excuse one. Pure and offline: it reads an already-fetched jobs payload plus
annotation strings. The thin `python -m todo_system.orchestrator.ci_status
<owner/repo> <run_id>` entry point is the only thing here that touches `gh`.
"""
from __future__ import annotations

import json
import subprocess
import sys

BUDGET_BLOCKED = "budget_blocked"
RUNNER_UNAVAILABLE = "runner_unavailable"
REAL_FAILURE = "real_failure"
GREEN = "green"
UNKNOWN = "unknown"

# Narrow phrases lifted from the two wordings GitHub has actually used for a dead
# Actions budget (2026-08-13 and 2026-08-18). Kept tight on purpose: a marker
# broad enough to hit a normal build log would launder a real failure into an
# excused one.
_BUDGET_MARKERS = ("actions budget", "budget is preventing", "spending limit",
                   "account payments have failed")

# Job conclusions that count as a failure. Matching explicitly (rather than
# "anything that isn't success") keeps a still-running or cancelled job from
# being read as evidence of failure.
_FAILING_CONCLUSIONS = ("failure", "timed_out", "startup_failure",
                        "action_required")


def _is_budget_annotation(messages: list[str]) -> bool:
    """True if any annotation blames an exhausted Actions budget / failed billing."""
    return any(mrk in (msg or "").lower()
               for msg in messages for mrk in _BUDGET_MARKERS)


def classify_ci_run(conclusion: str | None, jobs: list[dict] | None,
                    annotations: list[str] | None) -> str:
    """Label a finished run; `None` for jobs/annotations means "could not read"."""
    # `None` = the fetch itself failed (Jobs API 404, GraphQL 503). Guessing
    # `budget_blocked` from an unread run is exactly the wrong direction, so an
    # unreadable run gets its own verdict. `[]` is different: read, and empty.
    if jobs is None or annotations is None:
        return UNKNOWN
    failed = [j for j in jobs
              if (j.get("conclusion") or "").lower() in _FAILING_CONCLUSIONS]
    if not failed:
        # Nothing failed. Trust an explicit `success`; anything else (cancelled,
        # in-flight, a run-level startup failure with no job rows) leaves nothing
        # to attribute the red to.
        return GREEN if (conclusion or "").lower() == "success" else UNKNOWN
    # A job that ran steps and failed outranks a sibling that never started:
    # `budget_blocked` is the one verdict that excuses a red, so a run holding
    # any genuine failure must never claim it.
    if any(len(j.get("steps") or ()) > 0 for j in failed):
        return REAL_FAILURE
    # Every failing job has zero steps => no runner was ever acquired. `steps: 0`
    # alone says only that; the annotation is what attributes the cause, and
    # without a budget phrase this is the fixable kind.
    return BUDGET_BLOCKED if _is_budget_annotation(annotations) else RUNNER_UNAVAILABLE


def _gh_json(path: str):
    """Fetch one `gh api` path as JSON; `None` on any failure. Never raises."""
    try:
        r = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _fetch(repo: str, run_id: str) -> tuple:
    """Pull one run's conclusion, jobs and annotation messages. Fetch and unwrap
    only — no verdict is decided here. Whether the evidence is *sufficient* is
    decided here though: an unread read is passed on as `None`, and so is a
    partial one that turned up no budget phrase. Every failed read names itself
    on stderr; stdout stays reserved for the one-word verdict."""
    run = _gh_json(f"repos/{repo}/actions/runs/{run_id}")
    if not isinstance(run, dict):
        print(f"ci_status: could not read run {repo}#{run_id}", file=sys.stderr)
    conclusion = run.get("conclusion") if isinstance(run, dict) else None
    listing = _gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs")
    jobs = listing.get("jobs") if isinstance(listing, dict) else None
    if jobs is None:
        print(f"ci_status: could not read the jobs list for {repo}#{run_id}",
              file=sys.stderr)
        return conclusion, None, None
    messages: list[str] = []
    partial = False
    for job in jobs:
        # An Actions job id is also its check-run id, and annotations (where the
        # billing sentence lives) hang off the check-run, not off the job.
        got = _gh_json(f"repos/{repo}/check-runs/{job.get('id')}/annotations")
        if got is None:
            # The 404s and 503s behind this are intermittent on this account, so
            # one unreadable job is not a reason to drop the messages already in
            # hand. Skip it, keep collecting, and weigh the gap below.
            partial = True
            print("ci_status: could not read annotations for job "
                  f"{job.get('id')}", file=sys.stderr)
            continue
        messages += [a.get("message") or "" for a in got if isinstance(a, dict)]
    # A partial read is trusted asymmetrically, because the two zero-steps
    # verdicts rest on opposite kinds of evidence. `budget_blocked` is *positive*
    # — we saw the billing sentence — and a job we failed to read cannot un-see
    # it, so a partial read still carries it. `runner_unavailable` is *negative*
    # — the sentence is nowhere — and a partial read cannot establish "nowhere",
    # since the sentence may be sitting in the job that would not load. So that
    # case degrades to `None` (=> UNKNOWN) instead of guessing. Read every job
    # and `partial` is False, which skips this entirely: behaviour unchanged.
    if partial and not _is_budget_annotation(messages):
        return conclusion, jobs, None
    return conclusion, jobs, messages


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python -m todo_system.orchestrator.ci_status "
              "<owner/repo> <run_id>", file=sys.stderr)
        raise SystemExit(2)
    print(classify_ci_run(*_fetch(sys.argv[1], sys.argv[2])))
