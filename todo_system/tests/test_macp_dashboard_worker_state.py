"""Tests for scripts/macp_dashboard.py worker-state derivation — the "why is it
idle?" badge fields `worker_state` / `worker_state_detail`.

The core is `determine_worker_state(...)`, a pure function (no file/process/db
I/O): it takes the collected inputs and returns `(worker_state, detail)` per the
acceptance priority ladder

    paused > hold > budget > running > post_run > waiting_serial > no_work

These tests pin each of the 7 states, the ladder ordering (higher state wins when
several apply), the two 2026-10-13 real cases, and the two checkout helpers.

scripts/ has no __init__.py, so the module is loaded by path via importlib (its
top level is stdlib-only + a lazy todo_system import, so import is side-effect
free — the server only starts under `__main__`).
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
_SRC = VAULT / "scripts" / "macp_dashboard.py"


def _load():
    spec = importlib.util.spec_from_file_location("macp_dashboard", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()

_REPO = "https://example.invalid/acme/some-app.git"
_REPO2 = "https://example.invalid/acme/other-app.git"


def _call(**over):
    """determine_worker_state with sane 'nothing happening' defaults, overridable."""
    kw = dict(
        stage_count=0,
        in_flight=[],
        dispatchable_checkouts=[],
        paused=False,
        usage_allowed=True,
        runs_today=0,
        budget=40,
    )
    kw.update(over)
    return M.determine_worker_state(**kw)


class CheckoutHelpers(unittest.TestCase):
    def test_key_external_repo(self):
        self.assertEqual(M._checkout_key({"repo": _REPO}), _REPO)

    def test_key_vault_fallback(self):
        self.assertEqual(M._checkout_key(None), M._VAULT_CHECKOUT)
        self.assertEqual(M._checkout_key({}), M._VAULT_CHECKOUT)
        self.assertEqual(M._checkout_key({"repo": ""}), M._VAULT_CHECKOUT)

    def test_label_repo_name_from_url(self):
        self.assertEqual(M._checkout_label(_REPO), "some-app")
        self.assertEqual(M._checkout_label("git@host:acme/some-app.git"), "some-app")
        self.assertEqual(M._checkout_label("https://example.invalid/acme/x/"), "x")

    def test_label_vault_passthrough(self):
        self.assertEqual(M._checkout_label(M._VAULT_CHECKOUT), M._VAULT_CHECKOUT)


class WorkerStateLadder(unittest.TestCase):
    """One case per enum value + priority-ordering checks."""

    def _state(self, **over):
        return _call(**over)[0]

    def test_enum_is_seven(self):
        self.assertEqual(len(M.WORKER_STATES), 7)

    def test_all_results_are_in_enum(self):
        # every branch returns a member of the published enum
        for over in (
            {"paused": True},
            {"usage_allowed": False},
            {"runs_today": 41, "budget": 40},
            {"stage_count": 2},
            {"in_flight": [{"checkout": _REPO, "label": "some-app", "note": "n"}]},
            {"dispatchable_checkouts": [_REPO, _REPO]},
            {"dispatchable_checkouts": [_REPO]},
            {},
        ):
            self.assertIn(self._state(**over), M.WORKER_STATES)

    def test_paused(self):
        state, detail = _call(paused=True)
        self.assertEqual(state, "paused")
        self.assertIn("PAUSED", detail)

    def test_hold(self):
        state, detail = _call(usage_allowed=False)
        self.assertEqual(state, "hold")
        self.assertIn("HOLD", detail)

    def test_budget(self):
        state, detail = _call(runs_today=40, budget=40)
        self.assertEqual(state, "budget")
        self.assertIn("40/40", detail)

    def test_budget_not_reached_below_threshold(self):
        # 39/40 is not budget — falls through to no_work here
        self.assertEqual(self._state(runs_today=39, budget=40), "no_work")

    def test_budget_none_never_fires(self):
        # unknown budget (profile unreadable) must not claim 'budget reached'
        self.assertEqual(self._state(runs_today=999, budget=None), "no_work")

    def test_budget_zero_is_unlimited(self):
        # HQ runs with daily_run_budget 0 (unlimited) — never a budget hold
        self.assertEqual(self._state(runs_today=999, budget=0), "no_work")

    def test_running(self):
        state, detail = _call(stage_count=3)
        self.assertEqual(state, "running")
        self.assertIn("3", detail)

    def test_post_run_single(self):
        rows = [{"checkout": _REPO, "label": "some-app", "note": "베타 빌드 중"}]
        state, detail = _call(stage_count=0, in_flight=rows)
        self.assertEqual(state, "post_run")
        self.assertIn("some-app", detail)
        self.assertIn("베타 빌드", detail)

    def test_post_run_multi(self):
        rows = [
            {"checkout": _REPO, "label": "some-app", "note": "a"},
            {"checkout": _REPO2, "label": "other-app", "note": "b"},
        ]
        state, detail = _call(stage_count=0, in_flight=rows)
        self.assertEqual(state, "post_run")
        self.assertIn("2건", detail)
        self.assertIn("some-app", detail)
        self.assertIn("other-app", detail)

    def test_waiting_serial_contended(self):
        # two candidates share one checkout → only one can dispatch, other waits
        state, detail = _call(dispatchable_checkouts=[_REPO, _REPO])
        self.assertEqual(state, "waiting_serial")
        self.assertIn("some-app", detail)

    def test_waiting_serial_transition(self):
        # distinct free checkouts, nothing in-flight → pre-tick transition window
        state, detail = _call(dispatchable_checkouts=[_REPO, _REPO2])
        self.assertEqual(state, "waiting_serial")
        self.assertIn("디스패치 예정", detail)

    def test_no_work(self):
        state, detail = _call()
        self.assertEqual(state, "no_work")

    # ---- priority ordering (higher state wins when several apply) ----

    def test_paused_beats_budget(self):
        self.assertEqual(self._state(paused=True, runs_today=99, budget=40), "paused")

    def test_hold_beats_running(self):
        self.assertEqual(self._state(usage_allowed=False, stage_count=5), "hold")

    def test_budget_beats_running(self):
        self.assertEqual(self._state(runs_today=40, budget=40, stage_count=5), "budget")

    def test_running_beats_post_run(self):
        rows = [{"checkout": _REPO, "label": "some-app", "note": "n"}]
        self.assertEqual(self._state(stage_count=1, in_flight=rows), "running")

    def test_post_run_beats_waiting_serial(self):
        # in-flight row present → post_run even with same-repo candidate queued
        rows = [{"checkout": _REPO, "label": "some-app", "note": "n"}]
        self.assertEqual(
            self._state(in_flight=rows, dispatchable_checkouts=[_REPO]), "post_run")


class RealCases20261013(unittest.TestCase):
    """The two reported situations the badge should now explain."""

    def test_case1_budget_41_over_40(self):
        # 41 runs today against a 40 budget → budget, detail shows '41/40'
        state, detail = _call(runs_today=41, budget=40)
        self.assertEqual(state, "budget")
        self.assertIn("41/40", detail)

    def test_case2a_inflight_beta_build_post_run(self):
        # in-flight beta build (stage 0) + a same-repo candidate queued → post_run
        rows = [{"checkout": _REPO, "label": "some-app", "note": "beta publish build"}]
        state, detail = _call(
            stage_count=0, in_flight=rows, dispatchable_checkouts=[_REPO])
        self.assertEqual(state, "post_run")
        self.assertIn("some-app", detail)

    def test_case2b_same_repo_candidates_waiting_serial(self):
        # no in-flight row, two same-repo candidates compete for one checkout
        state, detail = _call(
            stage_count=0, in_flight=[], dispatchable_checkouts=[_REPO, _REPO])
        self.assertEqual(state, "waiting_serial")
        self.assertIn("some-app", detail)


class WorkerStateCap(unittest.TestCase):
    """leaf 5: max_parallel_per_repo flows into the serial-wait gloss. A checkout
    is 'contended' (someone waits) only when its queued candidates EXCEED the cap,
    and the detail names the cap. cap defaults to 1 (serial) for backward compat."""

    def test_two_candidates_fit_at_cap_2(self):
        # both same-repo candidates fit under cap=2 -> not contended; the pre-tick
        # transition gloss, not a serial-wait-on-each-other message
        state, detail = _call(dispatchable_checkouts=[_REPO, _REPO], cap=2)
        self.assertEqual(state, "waiting_serial")
        self.assertIn("디스패치 예정", detail)

    def test_three_candidates_contend_at_cap_2(self):
        state, detail = _call(dispatchable_checkouts=[_REPO, _REPO, _REPO], cap=2)
        self.assertEqual(state, "waiting_serial")
        self.assertIn("some-app 3건", detail)
        self.assertIn("repo당 최대 2개", detail)

    def test_default_cap_1_keeps_two_contended(self):
        # cap omitted -> default 1 -> two candidates still contend (unchanged behaviour)
        state, detail = _call(dispatchable_checkouts=[_REPO, _REPO])
        self.assertEqual(state, "waiting_serial")
        self.assertIn("some-app 2건", detail)
        self.assertIn("repo당 최대 1개", detail)


class CollectStatusSmoke(unittest.TestCase):
    """collect_status() wires the pure function to live data — assert the two new
    fields are present and well-formed against the real checkout's vault."""

    def test_status_carries_worker_state(self):
        d = M.collect_status()
        self.assertIn("worker_state", d)
        self.assertIn("worker_state_detail", d)
        self.assertIn(d["worker_state"], M.WORKER_STATES)
        self.assertIsInstance(d["worker_state_detail"], str)
        self.assertTrue(d["worker_state_detail"])
        # worker_busy kept for backward compatibility
        self.assertIn("worker_busy", d)


if __name__ == "__main__":
    unittest.main()
