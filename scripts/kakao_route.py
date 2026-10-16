#!/usr/bin/env python3
"""Shell shim for kakao noise-tier routing — kakao_send.sh calls this ONLY when
KAKAO_LEVEL / KAKAO_EVENT_KEY is set (the opt-in), so unrouted callers never
enter here. A separate file on purpose: kakao_send.sh's template heredoc lives
inside a $( ) command substitution where a lone apostrophe breaks the whole
script, so routing logic must not be inlined there.

stdin/argv:  argv[1] = the message.
env:         KAKAO_LEVEL (immediate|digest|silent, else immediate),
             KAKAO_EVENT_KEY (optional merge key), KAKAO_VAULT_DIR (state root).
stdout:      line 1 verdict SEND|QUEUED|SILENT|SUPPRESSED; for SEND the final
             message (possibly with an "외 N건" merge suffix) follows.
exit code:   always 0 — routing is best-effort like the send script itself; any
             failure prints SEND + the original message (an alert must never be
             lost to a routing bug).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

message = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    from todo_system.kakao_format import route
    verdict, final = route(
        message,
        level=os.environ.get("KAKAO_LEVEL"),
        event_key=os.environ.get("KAKAO_EVENT_KEY"),
    )
except Exception:
    verdict, final = "send", message

print({"send": "SEND", "digest": "QUEUED", "silent": "SILENT",
       "suppressed": "SUPPRESSED"}.get(verdict, "SEND"))
if verdict == "send":
    print(final if final is not None else message, end="")
