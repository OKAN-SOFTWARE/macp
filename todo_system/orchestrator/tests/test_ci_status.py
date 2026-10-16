"""Tests for red-CI classification — notably that `steps: 0` alone never proves a
budget block, and that an unreadable run stays `unknown`."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from todo_system.orchestrator import ci_status as cs

# Measured 2026-08-19 on `example-org/ai-stock-trading` main, run 32210155284
# (workflow `CI`, conclusion `failure`): one job `test`, zero steps, and this
# annotation. This is the live billing-death signature the classifier exists for.
_BUDGET_ANNOTATION = ("The job was not started because an Actions budget is "
                      "preventing further use.")
_BUDGET_JOBS = [{"id": 92112000001, "name": "test", "conclusion": "failure",
                 "steps": []}]

# Same zero-steps shape, different cause: a runner that was never acquired for a
# reason a human can actually fix.
_NO_RUNNER_ANNOTATION = ("No runner matching the specified labels was found: "
                         "self-hosted")

# A normal red: the runner started, ran nine steps, and a step failed.
_REAL_FAILURE_JOBS = [{"id": 92112000002, "name": "test", "conclusion": "failure",
                       "steps": [{"number": n, "conclusion":
                                  "failure" if n == 7 else "success"}
                                 for n in range(1, 10)]}]

_GREEN_JOBS = [{"id": 92112000003, "name": "test", "conclusion": "success",
                "steps": [{"number": n, "conclusion": "success"}
                          for n in range(1, 10)]}]

# Two zero-steps jobs, so `_fetch` has a second annotation read to lose when the
# first one 404s — the exact shape the partial-read gate is about.
_PARTIAL_JOBS = [{"id": 92112000011, "name": "build", "conclusion": "failure",
                  "steps": []},
                 {"id": 92112000012, "name": "test", "conclusion": "failure",
                  "steps": []}]


def _fake_gh(annotations, jobs=_PARTIAL_JOBS, conclusion="failure"):
    """A `_gh_json` stand-in that routes by path — no `gh`, no network.

    `annotations` maps job id -> the one message that check-run returns; mapping
    it to `None` is how an unreadable check-run is expressed (that is exactly
    what `_gh_json` hands back on a 404/503). `jobs=None` fails the listing."""
    def fake(path: str):
        if path.endswith("/jobs"):
            return None if jobs is None else {"jobs": jobs}
        if "/check-runs/" in path:
            job_id = int(path.split("/check-runs/")[1].split("/")[0])
            message = annotations.get(job_id)
            return None if message is None else [{"message": message}]
        return {"conclusion": conclusion}
    return fake


class BudgetBlockedTest(unittest.TestCase):
    def test_measured_budget_run_classifies_as_budget_blocked(self):
        self.assertEqual(
            cs.classify_ci_run("failure", _BUDGET_JOBS, [_BUDGET_ANNOTATION]),
            cs.BUDGET_BLOCKED)

    def test_older_billing_wording_also_matches(self):
        # The 2026-08-13 wording, before GitHub changed the sentence. Keying off
        # one sentence would have missed the other.
        msg = ("The job was not started because recent account payments have "
               "failed or your spending limit needs to be increased.")
        self.assertEqual(cs.classify_ci_run("failure", _BUDGET_JOBS, [msg]),
                         cs.BUDGET_BLOCKED)


class RunnerUnavailableTest(unittest.TestCase):
    def test_zero_steps_without_budget_wording_is_not_budget_blocked(self):
        # The core negative: `steps == 0` says only "no runner was acquired".
        # Reading it as a budget block would prescribe "wait for a human to pay"
        # for a label typo the worker could have fixed itself.
        verdict = cs.classify_ci_run("failure", _BUDGET_JOBS,
                                     [_NO_RUNNER_ANNOTATION])
        self.assertEqual(verdict, cs.RUNNER_UNAVAILABLE)
        self.assertNotEqual(verdict, cs.BUDGET_BLOCKED)

    def test_zero_steps_with_no_annotations_at_all_is_not_budget_blocked(self):
        # `[]` means "read the annotations, there were none" — still not evidence
        # of a budget block.
        verdict = cs.classify_ci_run("failure", _BUDGET_JOBS, [])
        self.assertEqual(verdict, cs.RUNNER_UNAVAILABLE)
        self.assertNotEqual(verdict, cs.BUDGET_BLOCKED)


class RealFailureTest(unittest.TestCase):
    def test_failing_job_with_steps_is_a_real_failure(self):
        self.assertEqual(
            cs.classify_ci_run("failure", _REAL_FAILURE_JOBS, []),
            cs.REAL_FAILURE)

    def test_real_failure_outranks_a_sibling_budget_blocked_job(self):
        # A mixed run must not be excused: one job never started (budget), but
        # another ran steps and failed, and that failure is the diff's own.
        verdict = cs.classify_ci_run("failure",
                                     _BUDGET_JOBS + _REAL_FAILURE_JOBS,
                                     [_BUDGET_ANNOTATION])
        self.assertEqual(verdict, cs.REAL_FAILURE)
        self.assertNotEqual(verdict, cs.BUDGET_BLOCKED)

    def test_budget_wording_inside_a_run_that_actually_executed(self):
        # A build log that merely mentions a budget is not a billing death.
        self.assertEqual(
            cs.classify_ci_run("failure", _REAL_FAILURE_JOBS,
                               ["assert cost <= spending limit"]),
            cs.REAL_FAILURE)


class GreenTest(unittest.TestCase):
    def test_successful_run_is_green(self):
        self.assertEqual(cs.classify_ci_run("success", _GREEN_JOBS, []),
                         cs.GREEN)

    def test_green_is_not_budget_blocked_even_with_budget_text_around(self):
        self.assertEqual(
            cs.classify_ci_run("success", _GREEN_JOBS, [_BUDGET_ANNOTATION]),
            cs.GREEN)


class UnknownTest(unittest.TestCase):
    def test_unreadable_jobs_is_unknown_not_budget_blocked(self):
        # Jobs API 404 / GraphQL 503 — both intermittent on this account. The
        # if_then constraint: never guess `budget_blocked` from an unread run.
        verdict = cs.classify_ci_run("failure", None, [_BUDGET_ANNOTATION])
        self.assertEqual(verdict, cs.UNKNOWN)
        self.assertNotEqual(verdict, cs.BUDGET_BLOCKED)

    def test_unreadable_annotations_is_unknown_not_budget_blocked(self):
        verdict = cs.classify_ci_run("failure", _BUDGET_JOBS, None)
        self.assertEqual(verdict, cs.UNKNOWN)
        self.assertNotEqual(verdict, cs.BUDGET_BLOCKED)

    def test_no_failing_job_but_not_success_is_unknown(self):
        # Cancelled run: nothing to attribute a red to.
        cancelled = [{"id": 1, "name": "test", "conclusion": "cancelled",
                      "steps": []}]
        self.assertEqual(cs.classify_ci_run("cancelled", cancelled, []),
                         cs.UNKNOWN)

    def test_empty_jobs_list_is_not_budget_blocked(self):
        self.assertNotEqual(
            cs.classify_ci_run("failure", [], [_BUDGET_ANNOTATION]),
            cs.BUDGET_BLOCKED)


class VerdictSetTest(unittest.TestCase):
    def test_every_verdict_is_one_of_the_five_constants(self):
        allowed = {cs.BUDGET_BLOCKED, cs.RUNNER_UNAVAILABLE, cs.REAL_FAILURE,
                   cs.GREEN, cs.UNKNOWN}
        cases = [
            ("failure", _BUDGET_JOBS, [_BUDGET_ANNOTATION]),
            ("failure", _BUDGET_JOBS, [_NO_RUNNER_ANNOTATION]),
            ("failure", _REAL_FAILURE_JOBS, []),
            ("success", _GREEN_JOBS, []),
            ("failure", None, None),
            (None, None, None),
            ("", [], []),
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertIn(cs.classify_ci_run(*case), allowed)


class FetchPartialAnnotationTest(unittest.TestCase):
    """`_fetch` when only some check-runs load. Offline: `_gh_json` is patched on
    the module, which is where `_fetch` looks it up."""

    def _fetch(self, annotations, **kw):
        with mock.patch.object(cs, "_gh_json", _fake_gh(annotations, **kw)):
            return cs._fetch("example-org/ai-stock-trading", "32210155284")

    def test_partial_read_still_reports_a_budget_block(self):
        # Job A's annotations 404 while job B carries the billing sentence.
        # The old early-return discarded B's message and collapsed to `unknown`.
        conclusion, jobs, messages = self._fetch(
            {92112000011: None,
             92112000012: _BUDGET_ANNOTATION})
        self.assertIsNotNone(messages)
        self.assertEqual(cs.classify_ci_run(conclusion, jobs, messages),
                         cs.BUDGET_BLOCKED)

    def test_partial_read_without_budget_wording_stays_unknown(self):
        # Same gap, no positive evidence. `runner_unavailable` would claim the
        # phrase is *nowhere*, which is precisely what the unread job could
        # falsify — so the annotations degrade to `None`, not to a guess.
        conclusion, jobs, messages = self._fetch(
            {92112000011: None,
             92112000012: _NO_RUNNER_ANNOTATION})
        self.assertIsNone(messages)
        self.assertEqual(cs.classify_ci_run(conclusion, jobs, messages),
                         cs.UNKNOWN)

    def test_unreadable_jobs_listing_still_returns_a_three_tuple(self):
        # Nothing to skip past here, and the arity stays 3 so the `__main__`
        # `classify_ci_run(*_fetch(...))` splat keeps working.
        self.assertEqual(self._fetch({}, jobs=None), ("failure", None, None))

    def test_partial_read_names_the_failing_job_on_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, _, messages = self._fetch(
                {92112000011: None,
                 92112000012: _BUDGET_ANNOTATION})
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("92112000011", lines[0])
        # ...and the evidence survived the diagnostic; stdout stays untouched,
        # reserved for the one-word verdict.
        self.assertIsNotNone(messages)

    def test_complete_read_keeps_every_message(self):
        # The control for "unchanged when every job reads": both messages come
        # back as a list, so the partial gate never fires.
        conclusion, jobs, messages = self._fetch(
            {92112000011: _NO_RUNNER_ANNOTATION,
             92112000012: _BUDGET_ANNOTATION})
        self.assertEqual(messages,
                         [_NO_RUNNER_ANNOTATION, _BUDGET_ANNOTATION])
        self.assertEqual(cs.classify_ci_run(conclusion, jobs, messages),
                         cs.BUDGET_BLOCKED)


if __name__ == "__main__":
    unittest.main()
