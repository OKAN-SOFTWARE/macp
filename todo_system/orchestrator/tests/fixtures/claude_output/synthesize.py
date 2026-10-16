"""Derive the synthesized stage-usage fixtures from the captured success.json.

Three outcomes cannot be produced on demand with a live call, so they are built
from success.json's real transcript lines instead, and labelled
`kind: synthesized` so no test mistakes them for CLI ground truth:

  error_during_execution  rc=1 with a transcript whose process died before it
                          wrote the cost-state line: usage is still recorded,
                          total_cost_usd is None
  capacity_limit          rc=1, a quota notice on stdout, and a transcript that
                          holds only the CLI's own `<synthetic>` notice
  cli_crash               rc=1 and no transcript at all

The stdout/stderr strings are illustrative, not captured CLI text. That is all
these need: the stage loop hands a stage's text on exactly as the CLI printed
it, so a test only has to compare SOME text before and after instrumentation.
Only the `text` and `text_sid` legs exist here — there is no JSON leg to fake.

    python3 todo_system/orchestrator/tests/fixtures/claude_output/synthesize.py

Session ids are uuid5 of the fixture name, so regenerating gives the same bytes.
Tests never import this module — they only read the JSON it writes.
"""

from __future__ import annotations

import copy
import json
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = "success"


def _dump(d: dict) -> str:
    # the CLI writes compact JSON; keep the same shape
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


def _retag(lines: list[dict], sid: str) -> list[dict]:
    out = []
    for d in lines:
        d = copy.deepcopy(d)
        if "sessionId" in d:
            d["sessionId"] = sid
        out.append(d)
    return out


def _synthetic_notice(template: dict, sid: str, text: str) -> dict:
    """An assistant line as the CLI writes its own error notice: `<synthetic>`
    model, zero usage, the notice as text."""
    d = copy.deepcopy(template)
    d["sessionId"] = sid
    d["isApiErrorMessage"] = True
    msg = d["message"]
    msg["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"macp-fixture/{sid}/notice"))
    msg["model"] = "<synthetic>"
    msg["content"] = [{"type": "text", "text": text}]
    msg["usage"] = {k: 0 for k in ("input_tokens", "output_tokens",
                                   "cache_creation_input_tokens", "cache_read_input_tokens")}
    return d


def _fixture(why: str, *, rc: int, stdout: str, stderr: str,
             transcript: list[dict] | None, base: dict, sid: str) -> dict:
    text_tail = base["text"]["argv_tail"]
    src_tail = base["text_sid"]["argv_tail"]
    i = src_tail.index("--session-id")
    sid_tail = [*src_tail[:i + 1], sid, *src_tail[i + 2:]]
    leg = {"rc": rc, "stdout": stdout, "stderr": stderr}
    return {
        "provenance": {
            "kind": "synthesized",
            "from": f"{SOURCE}.json (legs.text_sid transcript lines)",
            "why": why,
            "note": "stdout/stderr are illustrative, not captured CLI text",
            "regenerate": "python3 todo_system/orchestrator/tests/fixtures/"
                          "claude_output/synthesize.py",
        },
        "legs": {
            "text": {"argv_tail": text_tail, **leg},
            "text_sid": {"argv_tail": sid_tail, **leg, "session_id": sid,
                         # None = the CLI never wrote a transcript file
                         "transcript": None if transcript is None
                         else [_dump(d) for d in transcript]},
        },
    }


def build() -> dict[str, dict]:
    base = json.loads((HERE / f"{SOURCE}.json").read_text(encoding="utf-8"))["legs"]
    src = [json.loads(ln) for ln in base["text_sid"]["transcript"]]
    by_type = lambda t: [d for d in src if d.get("type") == t]  # noqa: E731
    prompt = by_type("queue-operation") + by_type("user")
    sid_of = {n: str(uuid.uuid5(uuid.NAMESPACE_URL, f"macp-fixture/{n}"))
              for n in ("error_during_execution", "capacity_limit", "cli_crash")}

    out = {}
    sid = sid_of["error_during_execution"]
    out["error_during_execution"] = _fixture(
        "cannot be triggered on demand; pins that an rc!=0 stage whose transcript "
        "has API messages but no cost-state still records its tokens",
        rc=1, stdout="Execution error", stderr="",
        transcript=_retag(prompt + by_type("assistant"), sid), base=base, sid=sid)

    sid = sid_of["capacity_limit"]
    notice = "You're out of usage credits · resets 6pm"
    out["capacity_limit"] = _fixture(
        "a quota refusal cannot be triggered on demand; the transcript then holds "
        "only the CLI's own <synthetic> notice, so there is no usage to record, "
        "while the stage loop must still take its capacity branch unchanged",
        rc=1, stdout=notice + "\n", stderr="",
        transcript=_retag(prompt, sid) + [_synthetic_notice(by_type("assistant")[0], sid, notice)],
        base=base, sid=sid)

    sid = sid_of["cli_crash"]
    out["cli_crash"] = _fixture(
        "a CLI that dies before starting a session writes no transcript",
        rc=1, stdout="", stderr="Error: illustrative CLI failure before any session\n",
        transcript=None, base=base, sid=sid)
    return out


def main() -> int:
    for name, fx in build().items():
        (HERE / f"{name}.json").write_text(
            json.dumps(fx, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{name}: rc={fx['legs']['text']['rc']} "
              f"transcript={'none' if fx['legs']['text_sid']['transcript'] is None else len(fx['legs']['text_sid']['transcript'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
