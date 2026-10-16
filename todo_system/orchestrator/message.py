"""Message envelope for the orchestrator mailbox.

An Envelope is the unit of dispatch (orchestrator -> agent) and reply
(agent -> orchestrator). It is transport-agnostic: the same JSON lands in a
file mailbox, a git mailbox, or (later) a broker. Stdlib-only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------

# Orchestrator -> agent (dispatch)
WAKE = "WAKE"
TASK_ASSIGN = "TASK_ASSIGN"
QUERY = "QUERY"
CANCEL = "CANCEL"
PING = "PING"

# Agent -> orchestrator (reply)
ACK = "ACK"
ACCEPT = "ACCEPT"
REJECT = "REJECT"
PROGRESS = "PROGRESS"
RESULT = "RESULT"
FAIL = "FAIL"
NEEDS_INPUT = "NEEDS_INPUT"
HEARTBEAT = "HEARTBEAT"

DISPATCH_TYPES = frozenset({WAKE, TASK_ASSIGN, QUERY, CANCEL, PING})
REPLY_TYPES = frozenset(
    {ACK, ACCEPT, REJECT, PROGRESS, RESULT, FAIL, NEEDS_INPUT, HEARTBEAT}
)
ALL_TYPES = DISPATCH_TYPES | REPLY_TYPES

ORCHESTRATOR = "orchestrator"

# ---------------------------------------------------------------------------
# ULID — 26-char, lexicographically sortable by time. Gives us globally
# time-ordered, collision-resistant message ids (and therefore filenames)
# without any external dependency.
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # base32, no I/L/O/U


def _encode(value: int, length: int) -> str:
    if not 0 <= value < 1 << (5 * length):
        raise ValueError(f"value {value} does not fit in {length} base32 chars")
    out = []
    for _ in range(length):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


_last_ms = 0
_last_rand = 0
_RAND_SPACE = 1 << 80
# The runner executes several runs on threads; two same-millisecond calls racing
# the read-modify-write below would mint one id twice, and the file mailbox
# names each envelope after its id — the second reply would overwrite the first.
_id_lock = threading.Lock()


def new_id() -> str:
    """Return a monotonic ULID string (48-bit ms time + 80-bit randomness).

    Ids are strictly increasing within a process, even across a clock step-back
    or an 80-bit randomness overflow within one millisecond.
    """
    global _last_ms, _last_rand
    with _id_lock:
        ms = int(time.time() * 1000)
        if ms <= _last_ms:
            # Same millisecond, or the clock stepped back (or we advanced past it
            # below): stay on _last_ms and increment to keep ids strictly increasing.
            ms = _last_ms
            rand = _last_rand + 1
            if rand >= _RAND_SPACE:
                # 80-bit space exhausted within this ms: borrow the next ms
                # instead of wrapping to 0, which would sort before earlier ids.
                ms += 1
                rand = int.from_bytes(os.urandom(10), "big")
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _last_ms = ms
        _last_rand = rand
    return _encode(ms, 10) + _encode(rand, 16)


def now_iso() -> str:
    """Local-time ISO-8601 with offset (e.g. 2026-07-20T08:30:00+09:00)."""
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass
class Envelope:
    to: str
    type: str
    frm: str = ORCHESTRATOR            # serialized as "from" (reserved word)
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    reply_to: str | None = None
    corr_id: str | None = None
    msg_id: str = field(default_factory=new_id)
    ts: str = field(default_factory=now_iso)
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.type not in ALL_TYPES:
            raise ValueError(f"unknown message type: {self.type!r}")
        if not self.to:
            raise ValueError("envelope.to is required")
        if self.idempotency_key is None:
            self.idempotency_key = self.msg_id
        if self.corr_id is None:
            # A dispatch starts a conversation; replies should carry the same
            # corr_id (caller sets it). Default a fresh conversation to msg_id.
            self.corr_id = self.msg_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "task_id": self.task_id,
            "from": self.frm,
            "to": self.to,
            "type": self.type,
            "reply_to": self.reply_to,
            "corr_id": self.corr_id,
            "ts": self.ts,
            "idempotency_key": self.idempotency_key,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Envelope":
        return cls(
            to=d["to"],
            type=d["type"],
            frm=d.get("from", ORCHESTRATOR),
            task_id=d.get("task_id"),
            payload=d.get("payload") or {},
            reply_to=d.get("reply_to"),
            corr_id=d.get("corr_id"),
            msg_id=d.get("msg_id") or new_id(),
            ts=d.get("ts") or now_iso(),
            idempotency_key=d.get("idempotency_key"),
        )

    @classmethod
    def from_json(cls, text: str) -> "Envelope":
        return cls.from_dict(json.loads(text))

    def reply(self, type: str, **kw: Any) -> "Envelope":
        """Build a reply to this message, preserving the conversation."""
        return Envelope(
            to=self.reply_to or self.frm,
            type=type,
            frm=self.to,
            task_id=self.task_id,
            reply_to=self.to,
            corr_id=self.corr_id,
            **kw,
        )
