"""HQ host guard — which host may dispatch, declared as one git-tracked line.

Two hosts share this vault repo (a mac and a GCP VM), and the
single-writer lease (`Worker/orchestrator.lock`) is gitignored, so it cannot
elect a dispatcher ACROSS hosts — each host happily grabs its own lease
(2026-09-11 pilot, `Todo/items/macp-cloud-hq-migration.md`). The marker file
`Worker/hq_host` names the one host that owns dispatch; it is tracked, so
handing over HQ (and rolling back) is a one-line commit.

Fail-open on purpose: a missing, empty, or unreadable marker means
"unguarded", never "locked". The worst failure mode of this guard is the HQ
locking ITSELF out and the whole queue silently stalling — so only a readable
marker naming a DIFFERENT host refuses. Callers gate on exit/return code 3
(the usage-guard convention for "blocked"); everything else proceeds.

Identity: `MACP_HOST_ID` env > `~/.macp/host_id` (one line) > gethostname().
The explicit file exists because macOS renames its hostname per network
(`.local` flapping); both real hosts already carry `~/.macp/host_id`.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

MARKER = "Worker/hq_host"


def local_host_id(home: Path | None = None) -> str:
    """This host's identity. `home` is a test seam — the real `~/.macp/host_id`
    exists on both hosts, so an uninjected call reads the machine's identity."""
    env = os.environ.get("MACP_HOST_ID", "").strip()
    if env:
        return env
    try:
        file_id = ((home or Path.home()) / ".macp" / "host_id").read_text(
            encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        file_id = ""
    if file_id:
        return file_id
    return socket.gethostname()


def check(root: str | Path = ".", local_id: str | None = None) -> tuple[bool, str]:
    """(ok, reason). ok=False ONLY for a readable marker naming another host."""
    marker = Path(root) / MARKER
    if not marker.exists():
        return True, f"no {MARKER} marker — unguarded"
    try:
        hq = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        # IsADirectoryError, permissions, mojibake: a broken marker is not a
        # foreign host — keep the queue moving.
        return True, f"warning: cannot read {MARKER} ({e}) — failing open"
    if not hq:
        return True, f"{MARKER} is empty — failing open"
    local = local_id if local_id is not None else local_host_id()
    if hq == local:
        return True, f"hq_host is '{hq}' — this host"
    return False, f"hq_host is '{hq}' but this host is '{local}' — refusing to dispatch"


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.hq_host")
    p.add_argument("--check", action="store_true",
                   help="print the verdict; exit 0 (allowed) or 3 (not the HQ host)")
    p.add_argument("--root", default=".", help="vault root holding Worker/hq_host")
    args = p.parse_args(argv)
    if not args.check:
        p.print_usage()
        return 2
    ok, reason = check(args.root)
    print(reason)
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(_main())
