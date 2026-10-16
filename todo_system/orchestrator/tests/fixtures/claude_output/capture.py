"""Capture live `claude -p` outputs as stage-usage test fixtures.

The runner's stage loop hands a stage's stdout to run.log, to the model-cache
invalidation hook and to the capacity check, and its stderr to the hook too.
Token usage has to be measured WITHOUT changing any of that, so each case runs
the SAME prompt as three legs and keeps every result verbatim:

  text      the argv the runner used before instrumentation (the baseline)
  text_sid  the same argv plus `--session-id <uuid4>` — design B, what
            build_stage_argv now emits; the usage is read back from the CLI's
            own transcript for that session id
  json      the same argv plus `--output-format json` — design A, rejected
            because this leg's stderr differs from `text` (the CLI drops its
            settings warnings in JSON mode), which would change what the
            model-cache hook reads. Kept as the evidence for that choice and as
            the CLI's own usage figures the transcript parser is checked against

Regenerate from the vault root (a stage's own cwd). Six live calls on the light
model, about a minute:

    python3 todo_system/orchestrator/tests/fixtures/claude_output/capture.py [case ...]

Each case writes `<case>.json` next to this file:

    {provenance, legs: {<leg>: {argv_tail, rc, stdout, stderr[, session_id, transcript]}}}

`argv_tail` is the argv after `claude -p <prompt>`. Decoding mirrors the runner's
`subprocess.run` exactly (text=True, utf-8, errors="replace"). `transcript` is the
session's JSONL, verbatim line by line but FILTERED to what the usage parser
reads (assistant + cost-state) plus a few small non-usage lines it must skip;
the attachment lines (system-prompt snapshots, CLAUDE.md, memory, skill and tool
listings) are private context and never leave the local config dir. Not `.log`:
the vault .gitignore drops `*.log`.

Tests never import this module — they only read the JSON it writes.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parents[4]          # claude_output → fixtures → tests → orchestrator → todo_system
sys.path.insert(0, str(VAULT))   # bootstrap so this standalone tool can import the engine
from todo_system.macp.accounts import legacy_config_dir  # noqa: E402  (needs sys.path above)
MODEL = "claude-haiku-4-5-20251001"
TIMEOUT_S = 180

# case → (prompt, argv appended LAST). `--allowed-tools` is variadic, so it has
# to come after the flags each leg inserts or it would swallow them.
CASES = {
    "success": ("Reply with exactly MACP_FIXTURE_OK and nothing else.", []),
    "error_max_turns": ("You must call the Read tool on CLAUDE.md before answering, "
                        "then reply with its first line.",
                        ["--allowed-tools", "Read"]),
}
# json first: if the stderr difference were an ordering effect ("the warning is
# printed once"), json would carry it here. It does not.
LEGS = ("json", "text", "text_sid")

# Transcript lines worth keeping: what the parser reads, and small lines it must
# skip. A user line is kept only when it is the prompt itself (str content),
# never a tool result (arbitrary file content).
_KEEP_TYPES = {"assistant", "cost-state", "queue-operation", "last-prompt"}
_KEEP_ATTACHMENTS = {"max_turns_reached"}


def _call(argv: list[str]) -> dict:
    r = subprocess.run(argv, cwd=str(VAULT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=TIMEOUT_S)
    return {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr}


def _cli_version() -> str:
    try:
        return _call(["claude", "--version"])["stdout"].strip()
    except Exception as e:                      # provenance only — never fatal
        return f"unknown ({type(e).__name__})"


def _keep(line: str) -> bool:
    try:
        d = json.loads(line)
    except ValueError:
        return False
    if not isinstance(d, dict):
        return False
    t = d.get("type")
    if t in _KEEP_TYPES:
        return True
    if t == "user":
        return isinstance((d.get("message") or {}).get("content"), str)
    if t == "attachment":
        return (d.get("attachment") or {}).get("type") in _KEEP_ATTACHMENTS
    return False


def _transcript(session_id: str | None) -> tuple[list[str], int]:
    """(kept lines, total line count) of the session's transcript, or ([], 0)."""
    if not session_id:
        return [], 0
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or legacy_config_dir())
    hits = sorted((root / "projects").glob(f"*/{session_id}.jsonl"))
    if not hits:
        return [], 0
    lines = hits[0].read_text(encoding="utf-8").splitlines()
    return [ln for ln in lines if _keep(ln)], len(lines)


def _json_session_id(stdout: str) -> str | None:
    try:
        d = json.loads(stdout)
    except ValueError:
        return None
    return d.get("session_id") if isinstance(d, dict) else None


def capture(case: str) -> dict:
    prompt, extra = CASES[case]
    base = ["--model", MODEL, "--max-turns", "1"]
    sid = str(uuid.uuid4())
    tails = {
        "text": [*base, *extra],
        "text_sid": [*base, "--session-id", sid, *extra],
        "json": [*base, "--output-format", "json", *extra],
    }
    legs: dict = {}
    for leg in LEGS:
        legs[leg] = {"argv_tail": tails[leg],
                     **_call(["claude", "-p", prompt, *tails[leg]])}
    kept_total: dict = {}
    for leg, leg_sid in (("text_sid", sid), ("json", _json_session_id(legs["json"]["stdout"]))):
        kept, total = _transcript(leg_sid)
        legs[leg]["session_id"] = leg_sid
        legs[leg]["transcript"] = kept
        kept_total[leg] = f"{len(kept)}/{total}"
    return {
        "provenance": {
            "kind": "captured",
            "captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "claude_version": _cli_version(),
            "model": MODEL,
            "cwd": "vault root",
            "prompt": prompt,
            "leg_order": list(LEGS),
            "transcript_lines_kept": kept_total,
            "regenerate": "python3 todo_system/orchestrator/tests/fixtures/"
                          f"claude_output/capture.py {case}",
        },
        "legs": {leg: legs[leg] for leg in ("text", "text_sid", "json")},
    }


def main(argv: list[str]) -> int:
    for case in argv or list(CASES):
        fx = capture(case)
        (HERE / f"{case}.json").write_text(
            json.dumps(fx, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        legs = fx["legs"]
        print(f"{case}: " + " ".join(
            f"{leg} rc={legs[leg]['rc']} out={len(legs[leg]['stdout'])}B "
            f"err={len(legs[leg]['stderr'])}B" for leg in legs)
            + f" transcript kept {fx['provenance']['transcript_lines_kept']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
