"""Tests for the human-decision route of report follow-ups (followups.py).

A rejection line carrying `"route": "human"` parks the follow-up as
awaiting_human instead of closing it, queues ONE digest line for it, and hands
it to the `followups human|approve|dismiss|reroute` CLI. The regression this
guards is the one that motivated it: a follow-up the planner could not decide
used to be closed as `rejected` and vanish without any person seeing it.

All five state paths — follow-ups, rejections, Todo items, and the kakao digest
queue + route log — are pinned onto a tempdir. The real ones (Worker/_state/,
logs/) are live state no test may touch, and conftest's KAKAO_VAULT_DIR net only
applies under pytest, not to a bare `python -m unittest` run of this file.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import kakao_format
from todo_system.orchestrator import followups

REPORT = """---
stage: report
---

## 다음 단계

- 후속 A: 토큰 계측 도입 여부 결정
- 후속 B: report 스테이지 탈모델화
- 후속 C: 재디스패치 효율화
- 후속 D: 문서 정리
"""

TS = "2026-01-01T00:00:00"


class FollowupsHumanRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.items = self.tmp / "items"
        self.items.mkdir()
        self._orig = (followups.FOLLOWUPS_PATH, followups.ITEMS_DIR,
                      followups.REJECTIONS_PATH, followups.DIGEST_PATH,
                      followups.ROUTE_LOG_PATH)
        followups.FOLLOWUPS_PATH = self.tmp / "_state" / "followups.jsonl"
        followups.ITEMS_DIR = self.items
        followups.REJECTIONS_PATH = self.tmp / "_state" / "followups_rejected.jsonl"
        followups.DIGEST_PATH = self.tmp / "logs" / "kakao_digest.jsonl"
        followups.ROUTE_LOG_PATH = self.tmp / "logs" / "kakao_route.log"

    def tearDown(self):
        (followups.FOLLOWUPS_PATH, followups.ITEMS_DIR, followups.REJECTIONS_PATH,
         followups.DIGEST_PATH, followups.ROUTE_LOG_PATH) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _append(self, task="demo", report=REPORT):
        return followups.append_followups(
            report, source_task=task, source_run="r1", ts=TS)

    def _reject(self, fid, reason="", **extra):
        """Append one planner rejection line (`route=` goes in via **extra)."""
        followups.REJECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with followups.REJECTIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": fid, "reason": reason, **extra},
                               ensure_ascii=False) + "\n")

    def _rec(self, fid):
        return {r["id"]: r for r in followups.load_followups()}[fid]

    def _digest(self):
        p = followups.DIGEST_PATH
        if not p.exists():
            return []
        return kakao_format.read_digest_entries(p.read_text(encoding="utf-8"))

    # ---- parking (AC1) ---------------------------------------------------

    def test_route_human_parks_awaiting_not_done(self):
        a, b, _, _ = self._append()
        self._reject(a, "user_decision_bound: 개선안 채택은 사람 결정", route="human")
        self._reject(b, "not actionable")
        self.assertEqual(sorted(followups.reconcile()), sorted([a, b]))
        ra = self._rec(a)
        self.assertEqual(ra["status"], followups.STATUS_HUMAN)
        self.assertEqual(ra["human_reason"],
                         "user_decision_bound: 개선안 채택은 사람 결정")
        self.assertIsNone(ra["resolution"])
        self.assertEqual([r["id"] for r in followups.awaiting_followups()], [a])
        self.assertNotIn(a, {r["id"] for r in followups.open_followups()})
        # the plain line beside it still closes exactly as it always did
        rb = self._rec(b)
        self.assertEqual((rb["status"], rb["resolution"]),
                         ("done", "rejected: not actionable"))

    def test_route_absent_or_other_value_is_plain_rejection(self):
        a, b, c, d = self._append()
        self._reject(a, "no route")
        self._reject(b, "machine route", route="machine")
        self._reject(c, "null route", route=None)
        self._reject(d, "shouty", route=" HUMAN ")
        followups.reconcile()
        for fid, reason in ((a, "no route"), (b, "machine route"), (c, "null route")):
            rec = self._rec(fid)
            self.assertEqual((rec["status"], rec["resolution"]),
                             ("done", f"rejected: {reason}"))
            self.assertNotIn("human_reason", rec)
        self.assertEqual(self._rec(d)["status"], followups.STATUS_HUMAN)

    def test_awaiting_closed_by_created_task(self):
        a, b, _, _ = self._append()
        self._reject(a, "사람 결정", route="human")
        followups.reconcile()
        (self.items / "picked.md").write_text(
            f"---\nfrom_followup: {a}\n---\n", encoding="utf-8")
        self.assertEqual(followups.reconcile(), [a])
        rec = self._rec(a)
        self.assertEqual((rec["status"], rec["resolution"]),
                         ("done", "created: picked"))
        # a created task also beats a human line landing in the same pass
        self._reject(b, "사람 결정", route="human")
        (self.items / "picked-b.md").write_text(
            f"---\nfrom_followup: {b}\n---\n", encoding="utf-8")
        self.assertEqual(followups.reconcile(), [b])
        self.assertEqual(self._rec(b)["resolution"], "created: picked-b")

    # ---- digest (AC3) ----------------------------------------------------

    def test_digest_one_line_per_id_dedup(self):
        a, *_ = self._append()
        self._reject(a, "사람 결정", route="human")
        self.assertEqual(followups.reconcile(), [a])
        [entry] = self._digest()
        self.assertEqual(entry["key"], f"followup-human:{a}")
        self.assertIn(a, entry["msg"])
        self.assertTrue(entry["msg"].startswith(f"{followups.HUMAN_DIGEST_HEAD}:"))
        self.assertTrue(followups.DIGEST_PATH.is_relative_to(self.tmp))
        self.assertIn("DIGEST", followups.ROUTE_LOG_PATH.read_text(encoding="utf-8"))
        self.assertTrue(self._rec(a).get("human_notified"))
        # a second reconcile, and a duplicate parking line, add nothing
        self.assertEqual(followups.reconcile(), [])
        self._reject(a, "사람 결정 (다시)", route="human")
        self.assertEqual(followups.reconcile(), [])
        self.assertEqual(len(self._digest()), 1)

    def test_digest_lines_fold_and_fit_limit(self):
        ids = self._append()[:3]
        for fid in ids:
            self._reject(fid, "사람 결정", route="human")
        followups.reconcile()
        entries = self._digest()
        self.assertEqual(sorted(e["key"] for e in entries),
                         sorted(f"followup-human:{i}" for i in ids))
        self.assertIn(f"{followups.HUMAN_DIGEST_HEAD} ×3",
                      kakao_format.compose_digest(entries))
        # a pathological slug and reason still fit, and the id survives both
        # this memo's clip and compose_digest's 60-char clip of a singleton
        slug = "macp-" + "-".join(["segment"] * 20)
        [lid] = self._append(task=slug, report="## 다음 단계\n\n- 긴 슬러그 후속\n")
        self._reject(lid, "사유 " * 100, route="human")
        followups.reconcile()
        msg = self._digest()[-1]["msg"]
        self.assertLessEqual(len(msg), kakao_format.KAKAO_TEXT_LIMIT)
        self.assertIn(f"[{lid}]", msg)
        self.assertIn(f"{followups.CLI} human", msg)
        self.assertIn(f"[{lid}]", kakao_format.compose_digest([{"msg": msg}]))

    def test_digest_failure_is_retried_not_sent(self):
        a, *_ = self._append()
        self._reject(a, "사람 결정", route="human")
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        good = followups.DIGEST_PATH
        followups.DIGEST_PATH = blocker / "kakao_digest.jsonl"
        real_route = kakao_format.route
        calls = []

        def spy(msg, **kw):
            out = real_route(msg, **kw)
            calls.append((kw.get("level"), out[0]))
            return out

        with mock.patch.object(followups.kakao_format, "route", side_effect=spy):
            self.assertEqual(followups.reconcile(), [a])   # the park still lands
            self.assertEqual(followups.reconcile(), [])    # retry fails the same way
        rec = self._rec(a)
        self.assertEqual(rec["status"], followups.STATUS_HUMAN)
        self.assertNotIn("human_notified", rec)
        # the router failed open to "send" both times — and nothing acted on it:
        # the only requests ever made were digest-level ones
        self.assertEqual(calls, [(kakao_format.LEVEL_DIGEST, "send")] * 2)
        followups.DIGEST_PATH = good
        self.assertEqual(followups.reconcile(), [])
        [entry] = self._digest()
        self.assertIn(a, entry["msg"])
        self.assertTrue(self._rec(a).get("human_notified"))

    def test_route_exception_never_breaks_reconcile(self):
        a, *_ = self._append()
        self._reject(a, "사람 결정", route="human")
        with mock.patch.object(followups.kakao_format, "route",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(followups.main(["reconcile"]), 0)
        rec = self._rec(a)
        self.assertEqual(rec["status"], followups.STATUS_HUMAN)
        self.assertNotIn("human_notified", rec)
        self.assertEqual(self._digest(), [])
        followups.reconcile()
        self.assertEqual(len(self._digest()), 1)

    # ---- planner injection (AC2, AC5, AC6) --------------------------------

    def _emit(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(followups.main(["emit-context"]), 0)
        return buf.getvalue()

    def test_awaiting_is_not_injected(self):
        a, b, c, d = self._append()
        for fid in (a, b, c, d):
            self._reject(fid, "사람 결정", route="human")
        followups.reconcile()
        # nothing but parked rows → the byte-identical empty (no-injection) path
        self.assertEqual(self._emit(), "")
        self.assertEqual(followups.render_context(followups.load_followups()), "")
        # a mix → only the open one is injected
        [e] = self._append(report="## 다음 단계\n\n- 새 후속 E\n")
        out = self._emit()
        self.assertIn(e, out)
        for fid in (a, b, c, d):
            self.assertNotIn(fid, out)
        # …even when a caller hands render_context every row
        ctx = followups.render_context(followups.load_followups())
        self.assertIn(e, ctx)
        self.assertNotIn(a, ctx)

    def test_protocol_text_explains_route_human(self):
        self._append()
        ctx = self._emit()
        self.assertIn('"route":"human"', ctx)
        self.assertIn("다른 채널이 아직 없을 때만", ctx)
        self.assertIn("[검토요청]", ctx)
        self.assertIn("auto_merge:false", ctx)
        # the original protocol is still there, word for word
        self.assertIn('`{"id":"<id>","reason":"..."}`를 append 하라.', ctx)
        # planner_tick counts `^- [` lines as follow-ups — prose must never match
        self.assertEqual(sum(ln.startswith("- [") for ln in ctx.splitlines()), 4)

    def test_render_approved_block_is_separate(self):
        base = {"section": "다음 단계", "source_task": "demo", "status": "open"}
        recs = [dict(base, id="aaaaaaaaaaaa", text="보통 후속"),
                dict(base, id="bbbbbbbbbbbb", text="승인된 후속", approved=True)]
        ctx = followups.render_context(recs)
        head = "### 사람 승인됨 — 태스크화 대상"
        self.assertIn(head, ctx)
        before, after = ctx.split(head)
        self.assertIn("aaaaaaaaaaaa", before)
        self.assertNotIn("bbbbbbbbbbbb", before)
        self.assertIn("- [bbbbbbbbbbbb]", after)
        self.assertIn("user_decision_bound", after)
        self.assertEqual(sum(ln.startswith("- [") for ln in ctx.splitlines()), 2)
        # no approved row → no approved block
        self.assertNotIn(head, followups.render_context(recs[:1]))

    # ---- human CLI (AC4, AC5) ---------------------------------------------

    def _cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = followups.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def _park(self, fid, reason="사람 결정"):
        self._reject(fid, reason, route="human")
        followups.reconcile()
        self.assertEqual(self._rec(fid)["status"], followups.STATUS_HUMAN)

    def test_cli_human_lists_awaiting(self):
        a, b, _, _ = self._append(task="macp-pipeline-stage-cost-benefit")
        rc, out, _ = self._cli("human")
        self.assertEqual((rc, out), (0, "사람 결정 대기 없음\n"))
        self._park(a, "개선안 채택은 사람 결정 (user_decision_bound)")
        # `human` is read-only: a pending line for b is NOT applied by listing
        self._reject(b, "사람 결정", route="human")
        before = followups.FOLLOWUPS_PATH.read_bytes()
        rc, out, _ = self._cli("human")
        self.assertEqual(rc, 0)
        self.assertEqual(followups.FOLLOWUPS_PATH.read_bytes(), before)
        self.assertIn(f"[{a}]", out)
        self.assertIn("macp-pipeline-stage-cost-benefit", out)
        self.assertIn("토큰 계측 도입 여부 결정", out)
        self.assertIn("개선안 채택은 사람 결정 (user_decision_bound)", out)
        self.assertNotIn(b, out)
        self.assertIn(f"{followups.CLI} approve <id>", out)
        self.assertIn(f"{followups.CLI} dismiss <id> --reason", out)

    def test_cli_approve_reopens_and_injects_approved_block(self):
        a, *_ = self._append()
        self._park(a)
        rc, out, _ = self._cli("approve", f"[{a}]")   # brackets as `human` prints
        self.assertEqual(rc, 0)
        self.assertIn("24", out)                      # the latency is spelled out
        rec = self._rec(a)
        self.assertEqual(rec["status"], "open")
        self.assertIs(rec["approved"], True)
        self.assertTrue(rec["approved_ts"])
        self.assertEqual(rec["rejections_seen"], 1)
        ctx = self._emit()
        head = "사람 승인됨 — 태스크화 대상"
        self.assertIn(head, ctx)
        self.assertIn(f"- [{a}]", ctx.split(head, 1)[1])
        self.assertIn("user_decision_bound", ctx.split(head, 1)[1])
        _, listing, _ = self._cli("human")
        self.assertIn("승인됨 — planner 태스크화 대기 1건", listing)

    def test_approve_not_undone_by_preapproval_rejection(self):
        a, b, _, _ = self._append()
        self._park(a)
        self.assertEqual(self._cli("approve", a)[0], 0)
        # the line that parked it is still in the append-only file — no bounce
        self.assertEqual(followups.reconcile(), [])
        rec = self._rec(a)
        self.assertEqual((rec["status"], rec.get("approved")), ("open", True))
        # the planner parks it again with a NEW human line: parked, but the
        # one-line-per-id digest promise holds
        self._reject(a, "여전히 사람 결정", route="human")
        self.assertEqual(followups.reconcile(), [a])
        self.assertEqual(self._rec(a)["status"], followups.STATUS_HUMAN)
        self.assertEqual(len(self._digest()), 1)
        self.assertEqual(self._cli("approve", a)[0], 0)
        self.assertEqual(followups.reconcile(), [])
        # a NEW plain line after the approve (another blocked pattern) still closes it
        self._reject(a, "external_action: 콘솔 조작")
        self.assertEqual(followups.reconcile(), [a])
        rec = self._rec(a)
        self.assertEqual((rec["status"], rec["resolution"]),
                         ("done", "rejected: external_action: 콘솔 조작"))
        # approve + a from_followup task closes as created
        self._park(b)
        self.assertEqual(self._cli("approve", b)[0], 0)
        (self.items / "from-b.md").write_text(
            f"---\nfrom_followup: {b}\n---\n", encoding="utf-8")
        self.assertEqual(followups.reconcile(), [b])
        self.assertEqual(self._rec(b)["resolution"], "created: from-b")
        self.assertEqual(len(self._digest()), 2)      # one for a, one for b

    def test_cli_dismiss_closes_with_dismissed_resolution(self):
        a, b, _, _ = self._append()
        self._park(a)
        self._park(b)
        # no reason → usage error, row untouched
        self.assertEqual(self._cli("dismiss", a)[0], 2)
        self.assertEqual(self._cli("dismiss", a, "--reason")[0], 2)
        self.assertEqual(self._cli("dismiss", a, "--reason=")[0], 2)
        self.assertEqual(self._rec(a)["status"], followups.STATUS_HUMAN)
        rc, out, _ = self._cli("dismiss", a, "--reason", "안", "하기로")
        self.assertEqual(rc, 0)
        rec = self._rec(a)
        self.assertEqual((rec["status"], rec["resolution"]),
                         ("done", "dismissed: 안 하기로"))
        self.assertEqual(self._cli("dismiss", b, "--reason=이미 처리됨")[0], 0)
        self.assertEqual(self._rec(b)["resolution"], "dismissed: 이미 처리됨")
        # closed for good: reconcile never revives it
        self._reject(a, "사람 결정", route="human")
        self.assertEqual(followups.reconcile(), [])
        self.assertEqual(self._rec(a)["status"], "done")

    def test_cli_errors_exit_codes(self):
        a, b, _, _ = self._append()
        self._park(a)
        self.assertEqual(self._cli("approve", "000000000000")[0], 1)
        self.assertEqual(self._cli("dismiss", "000000000000", "--reason", "x")[0], 1)
        self.assertEqual(self._cli("reroute", "000000000000")[0], 1)
        rc, _, err = self._cli("approve", b)             # open, not parked
        self.assertEqual(rc, 1)
        self.assertIn("status='open'", err)
        self.assertEqual(self._cli("dismiss", b, "--reason", "x")[0], 1)
        self.assertEqual(self._rec(b)["status"], "open")
        self.assertEqual(self._cli("approve")[0], 2)
        self.assertEqual(self._cli("approve", a, b)[0], 2)
        self.assertEqual(self._cli("reroute")[0], 2)
        self.assertEqual(self._cli("bogus")[0], 2)
        self.assertEqual(self._cli()[0], 2)
        self.assertEqual(self._rec(a)["status"], followups.STATUS_HUMAN)

    def test_reroute_revives_rejected_only(self):
        a, b, c, d = self._append()
        # a: an old-style rejection (row closed by the pre-route code path)
        self._reject(a, "user_decision_bound: human decision")
        followups.mark_done([a], "rejected: user_decision_bound: human decision")
        rc, out, _ = self._cli("reroute", a)
        self.assertEqual(rc, 0)
        rec = self._rec(a)
        self.assertEqual(rec["status"], followups.STATUS_HUMAN)
        self.assertEqual(rec["human_reason"], "user_decision_bound: human decision")
        self.assertIsNone(rec["resolution"])
        self.assertTrue(rec["rerouted_ts"])
        [entry] = self._digest()
        self.assertEqual(entry["key"], f"followup-human:{a}")
        self.assertTrue(rec.get("human_notified"))
        # approving a rerouted row is not undone by its old plain line
        self.assertEqual(self._cli("approve", a)[0], 0)
        self.assertEqual(followups.reconcile(), [])
        self.assertEqual(self._rec(a)["status"], "open")
        # refused: created, dismissed, still open, parked
        (self.items / "t-b.md").write_text(
            f"---\nfrom_followup: {b}\n---\n", encoding="utf-8")
        followups.reconcile()
        self._park(c)
        self.assertEqual(self._cli("dismiss", c, "--reason", "no")[0], 0)
        self.assertEqual(self._cli("reroute", b)[0], 1)
        self.assertEqual(self._cli("reroute", c)[0], 1)
        self.assertEqual(self._cli("reroute", d)[0], 1)
        self._park(d)
        self.assertEqual(self._cli("reroute", d)[0], 1)
        self.assertEqual(self._rec(b)["resolution"], "created: t-b")
        self.assertEqual(self._rec(c)["resolution"], "dismissed: no")

    # ---- no backfill (AC7) -----------------------------------------------

    def test_no_backfill_of_existing_rejections(self):
        a, b, _, _ = self._append()
        # history from before the human route existed: plain lines, rows already
        # closed by the old code (no watermark field on them)
        self._reject(a, "user_decision_bound: human decision")
        self._reject(b, "human-gated")
        followups.mark_done([a], "rejected: user_decision_bound: human decision")
        followups.mark_done([b], "rejected: human-gated")
        self.assertNotIn("rejections_seen", self._rec(a))
        before = followups.FOLLOWUPS_PATH.read_bytes()
        # a NEW human-routed line naming an already-rejected id revives nothing
        self._reject(a, "사람 결정", route="human")
        self.assertEqual(followups.reconcile(), [])
        self.assertEqual(followups.FOLLOWUPS_PATH.read_bytes(), before)
        self.assertFalse(followups.DIGEST_PATH.exists())
        self.assertEqual(self._rec(a)["status"], "done")


if __name__ == "__main__":
    unittest.main()
