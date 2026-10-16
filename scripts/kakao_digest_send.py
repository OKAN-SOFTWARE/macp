#!/usr/bin/env python3
"""Flush the kakao digest queue as ONE daily memo (macp-kakao-digest.timer).

Digest-tier alerts (scripts/KAKAO_ALERTS.md) are never sent individually —
kakao_format.route() appends them to logs/kakao_digest.jsonl and this sender,
run once a day by the systemd --user timer gen_units.py emits, delivers the
whole queue as a single self-limiting memo. Deliberately NOT the morning brief:
brief.sh runs a Claude call that has died to quota exhaustion before
(brief-dies-before-weekly-quota-reset), and the queue must not lose entries to
someone else's outage.

No-loss contract, in order:
  1. read the queue and remember how many BYTES were consumed;
  2. send via kakao_send.sh with KAKAO_SEND_STRICT=1 (a failed send exits 1);
  3. only on success: archive the consumed bytes to logs/kakao_digest.sent.jsonl
     and rewrite the queue with whatever arrived DURING the send (byte offset,
     under the same flock route()'s appends take).
A failure leaves the queue byte-identical — the next timer run retries. A crash
between send-success and archive re-sends tomorrow: duplication over loss.

stdlib-only: runs under bare /usr/bin/python3 from systemd, like health_report.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_INSTALL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_INSTALL_DIR))          # bootstrap — raw install location
from todo_system import paths  # noqa: E402
from todo_system.kakao_format import compose_digest, read_digest_entries  # noqa: E402
# Engine root through the resolver: dev checkout -> repo root (same value as the
# bootstrap above); wheel install -> todo_system/_engine, where scripts/ and
# .claude/commands ship in repo layout. The bootstrap only puts the engine on
# sys.path; this is the definition the rest of the module uses.
_INSTALL_DIR = paths.engine_root()

# KAKAO_VAULT_DIR keeps its historical top priority (ahead of MACP_VAULT_ROOT);
# only the fallback is now the resolver, anchored at paths.vault_search_start():
# the engine root in a checkout (byte-identical to the former
# Path(__file__).resolve().parents[1] when neither env is set), None in an
# installed wheel so the cwd-upward search runs instead of a site-packages anchor.
VAULT = (
    Path(os.environ["KAKAO_VAULT_DIR"])
    if os.environ.get("KAKAO_VAULT_DIR")
    else paths.vault_root(start=paths.vault_search_start())
)

QUEUE = VAULT / "logs" / "kakao_digest.jsonl"
SENT = VAULT / "logs" / "kakao_digest.sent.jsonl"
# Engine file: resolve from the install dir, not the vault (split install).
SEND_SH = _INSTALL_DIR / "scripts" / "kakao_send.sh"


def _flock(fh) -> None:
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX)
    except Exception:
        pass


def default_send(text: str) -> bool:
    """One strict kakao send. KAKAO_LEVEL/KAKAO_EVENT_KEY are stripped so the
    digest memo itself can never be routed back into the queue it drains."""
    env = {k: v for k, v in os.environ.items() if k not in ("KAKAO_LEVEL", "KAKAO_EVENT_KEY")}
    env["KAKAO_SEND_STRICT"] = "1"
    # Route the Slack fanout of the digest memo to its own 'digest' channel
    # (slack_send.py reads NOTIFY_CHANNEL_KEY first). The Kakao path is untouched.
    env["NOTIFY_CHANNEL_KEY"] = "digest"
    try:
        r = subprocess.run(["bash", str(SEND_SH), text],
                           env=env, capture_output=True, timeout=120)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def flush(queue_path: Path = QUEUE, sent_path: Path = SENT,
          send=default_send, log=print) -> int:
    """Deliver the queue as one memo. Returns 0 on success or empty queue,
    1 when the send failed (queue preserved for the next run)."""
    if not queue_path.exists():
        log("kakao-digest: queue empty (no file) — nothing to send")
        return 0
    with open(queue_path, "rb") as fh:
        _flock(fh)
        raw = fh.read()
    consumed = len(raw)
    entries = read_digest_entries(raw.decode("utf-8", errors="replace"))
    if not entries:
        log("kakao-digest: queue empty — nothing to send")
        return 0

    text = compose_digest(entries)
    if not send(text):
        log(f"kakao-digest: send FAILED — queue preserved ({len(entries)} entries), "
            "will retry on the next timer run")
        return 1

    # Success: archive exactly the bytes that went out, keep what arrived since.
    with open(queue_path, "rb+") as fh:
        _flock(fh)
        full = fh.read()
        sent_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sent_path, "ab") as archive:
            archive.write(full[:consumed])
        fh.seek(0)
        fh.write(full[consumed:])
        fh.truncate()
    log(f"kakao-digest: sent {len(entries)} entries in one memo "
        f"({len(full) - consumed} bytes arrived during send, kept queued)")
    return 0


if __name__ == "__main__":
    sys.exit(flush())
