"""Single-writer lease so only one orchestrator drives at a time.

File-based, sufficient for the Mac-single instance. A future cloud takeover
shares this file (via git or a shared store) and acquires once the Mac's lease
expires — the seam that keeps the design cloud-ready without running two
orchestrators at once. Stdlib-only.

Acquisition is ATOMIC: `O_CREAT|O_EXCL` (not "read, decide, write") settles who
wins, so concurrent callers can never both see "free". A lock file we cannot
parse counts as HELD, never as free — the old code collapsed "no file" and
"unparseable file" into one `None` and handed the lock out on both. The one way
out of an unparseable lock is its mtime: it backstops an unreadable lock exactly
as `expires_at` backstops a readable one, so a 0-byte lock cannot wedge the
planner tick forever.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_write_text

# The dedicated CHECKOUT lease (distinct from Worker/orchestrator.lock): it
# serializes ONLY the live vault's HEAD-moving branch prep against the planner
# tick's claude call, so the planner can no longer self-commit onto a worker
# branch mid-branch-prep. A far longer TTL than the orchestrator lease because
# the planner holds it across a whole `claude -p` call (its own timeout is 600s);
# the TTL is only the backstop for a holder that dies without releasing, with the
# same-host dead-PID reclaim below clearing it sooner in the common crash case.
CHECKOUT_LEASE_PATH = "Worker/checkout.lock"
CHECKOUT_LEASE_TTL = 1200.0


def _pid_alive(pid: int) -> bool:
    """Signal-0 liveness probe. True when a process with `pid` exists on THIS
    host. Only meaningful for a same-host pid — a caller must never probe a pid it
    read from another host's holder string (see `_reclaimable`).

    Fail-safe toward alive: only a definite ProcessLookupError reports "gone", so
    an unreadable/insane pid keeps the lock rather than reclaiming it wrongly."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False           # no such process — the holder is gone
    except PermissionError:
        return True            # exists but owned by another user
    except (OSError, OverflowError, ValueError):
        return True            # cannot tell → assume alive
    return True


def _parse_holder(holder: str | None) -> tuple[str | None, int | None]:
    """`"<host>:<pid>"` → `(host, pid)`; `(host, None)` when the pid is not an int,
    `(None, None)` when there is no `:` at all. `rpartition` so a host that itself
    contains a colon still splits on the LAST colon (the pid)."""
    if not holder or ":" not in holder:
        return None, None
    host, _, pid_s = holder.rpartition(":")
    try:
        return (host or None), int(pid_s)
    except ValueError:
        return (host or None), None


@dataclass
class LeaseInfo:
    holder: str
    expires_at: float


# The three things the lock file on disk can say. Keeping _ABSENT and _UNKNOWN
# apart is the whole point: they are opposite facts ("nobody holds it" vs
# "somebody holds it and I cannot read who"), and the old `_read` returned the
# same `None` for both.
_ABSENT = "absent"
_UNKNOWN = "unknown"
_HELD = "held"


class Lease:
    def __init__(self, path: str | Path, holder: str | None = None, ttl: float = 120.0):
        self.path = Path(path)
        self.holder = holder or f"{socket.gethostname()}:{os.getpid()}"
        self.ttl = ttl

    def _read_state(self) -> tuple[str, LeaseInfo | None]:
        """What the file on disk says, with "missing" and "unreadable" kept apart.

        No `self.path.exists()` pre-check: that is its own little check-then-use
        window, and one read syscall already answers both questions at a single
        instant. `FileNotFoundError` IS the absence check.

        `TypeError` in the parse clause is load-bearing, not defensive padding:
        `expires_at: null` reaches `float(None)` and a JSON array payload reaches
        `["a"]["holder"]`, both `TypeError`, and the old clause did not catch it —
        so the exception escaped `_main`, whose rc then lands in
        `planner_tick.sh`'s "rc is neither 0 nor 1" branch, which fails OPEN and
        runs the planner with no lock at all."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _ABSENT, None
        except OSError:
            return _UNKNOWN, None              # EACCES, EIO, a directory, …
        try:
            d = json.loads(raw)
            return _HELD, LeaseInfo(d["holder"], float(d["expires_at"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return _UNKNOWN, None

    def _read(self) -> LeaseInfo | None:
        """`LeaseInfo` or None for `current()`/`release()`, which only ever act on
        a holder they can positively read. Acquisition must NOT use this — it
        cannot tell absent from unreadable; use `_read_state`."""
        return self._read_state()[1]

    def _payload(self, now: float) -> str:
        return json.dumps({"holder": self.holder, "expires_at": now + self.ttl},
                          indent=2)

    def _write(self, now: float) -> None:
        """Renewal only — the lock is already ours, so replacing it is legitimate.
        Routed through the shared atomic helper so a renewal cannot leave a torn
        lock behind: under the read policy above, a torn lock we wrote ourselves
        would lock US out of our own lease until the mtime backstop expired."""
        atomic_write_text(self.path, self._payload(now))

    def _unknown_is_stale(self, now: float) -> bool:
        """The only way out of an unreadable lock.

        Dying between the `O_EXCL` create and the write leaves a 0-byte lock whose
        `expires_at` cannot be read, so "unknown → held" on its own would make
        every later `acquire` return False FOREVER (the planner tick skipping for
        good). mtime does for an unreadable lock what `expires_at` does for a
        readable one. A stat we cannot do is held, same fail-safe direction."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        return (now - mtime) > self.ttl

    def _reclaimable(self, cur: LeaseInfo, now: float) -> bool:
        """True when a lock we CAN read is dead and may be taken over."""
        if cur.expires_at <= now:
            return True                        # TTL expired
        # Same-host dead-PID reclaim: a lock a crashed process left behind must
        # not wedge dispatch for the whole (deliberately long) checkout TTL. Only
        # a POSITIVE "the pid is gone" verdict on THIS host reclaims; a remote
        # host (unprobeable), an unparseable holder, or a live pid all keep the
        # lock. A wrong reclaim would run two branch preps over one checkout, so
        # the bias is toward holding — TTL is the backstop for every other case.
        host, pid = _parse_holder(cur.holder)
        me_host, _ = _parse_holder(self.holder)
        return (host is not None and host == me_host and pid is not None
                and not _pid_alive(pid))

    def _create_exclusive(self, now: float) -> bool:
        """THE acquisition primitive. `O_CREAT|O_EXCL` is settled by the kernel in
        one step, so of N concurrent callers exactly one creates the file — and no
        read is consulted, which is why the check-then-write window is gone rather
        than merely narrowed.

        Writes into the fd instead of tmp→`os.replace`: a replace would clobber an
        existing lock and throw away the exclusivity we just won. That does leave a
        window where the file exists but is still 0 bytes, i.e. unreadable — and a
        reader calling that "held" is CORRECT, because we do hold it. Dying inside
        the window is what `_unknown_is_stale` exists for.

        Every failure is False, never an exception: `_main`'s rc must stay in
        {0, 1} or `planner_tick.sh` fails open."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False                       # someone else holds it
        except OSError:
            return False                       # cannot create → claim nothing
        try:
            os.write(fd, self._payload(now).encode("utf-8"))
            os.fsync(fd)
        except OSError:
            return False                       # 0-byte lock stays; mtime clears it
        finally:
            os.close(fd)
        return True

    def _takeover(self, now: float) -> bool:
        """Reclaim a lock already judged dead — via `os.rename`, NOT `unlink`.

        `unlink` → `create` reopens the very race we are closing: A unlinks and
        creates, then B — which judged the SAME old lock dead a moment earlier —
        unlinks A's brand new lock and creates its own, and there are two holders
        again. A rename can consume its source only once, so of N processes that
        all called one lock reclaimable, exactly one rename succeeds and the rest
        get an OSError and claim nothing. Losing the following create to a third
        party is fine too — that is just "not acquired". No interleaving yields
        two holders."""
        stale = self.path.with_name(
            f"{self.path.name}.stale.{os.getpid()}.{time.time_ns()}")
        try:
            os.rename(self.path, stale)
        except OSError:
            return False                       # another taker consumed it first
        try:
            return self._create_exclusive(now)
        finally:
            try:
                os.unlink(stale)               # leave no debris beside the lock
            except OSError:
                pass

    def acquire(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self._create_exclusive(now):
            return True                        # nobody had it — atomic win
        # The lock exists. From here on every branch either renews something
        # already ours or goes through an explicit takeover; none of them decides
        # "it is free" from a read.
        state, cur = self._read_state()
        if state == _ABSENT:
            # It vanished between our create attempt and this read — a release
            # landed in between. One retry, still through the atomic primitive.
            return self._create_exclusive(now)
        if state == _UNKNOWN:
            # Unreadable: held, unless it has been unreadable past the TTL.
            return self._takeover(now) if self._unknown_is_stale(now) else False
        if cur.holder == self.holder:
            self._write(now)                   # renew: already ours, atomic replace
            return True
        if self._reclaimable(cur, now):
            return self._takeover(now)
        return False

    # renew is acquire's twin; kept separate for call-site clarity.
    renew = acquire

    def release(self) -> None:
        cur = self._read()
        if cur and cur.holder == self.holder:
            try:
                self.path.unlink()
            except OSError:
                pass

    def current(self) -> LeaseInfo | None:
        return self._read()


def _main(argv: list[str] | None = None) -> int:
    """Bash-callable entry point so a shell (planner_tick.sh) can take the
    checkout lease WITHOUT reinventing it as `flock` — the reuse the task
    requires. `python -m todo_system.orchestrator.lease <action> --path <lock>
    [--ttl N] [--holder H]`.

    acquire / renew: rc 0 when taken, rc 1 when another live holder owns it (its
    id is printed so the caller can log who). release / current: rc 0. The holder
    defaults to `<host>:<pid>`, whose pid the same-host reclaim above can probe."""
    import argparse

    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.lease")
    p.add_argument("action", choices=["acquire", "renew", "release", "current"])
    p.add_argument("--path", required=True, help="lock file path")
    p.add_argument("--ttl", type=float, default=CHECKOUT_LEASE_TTL,
                   help=f"seconds until the lock self-expires (default {CHECKOUT_LEASE_TTL:.0f})")
    p.add_argument("--holder", default=None, help='owner id (default "<host>:<pid>")')
    args = p.parse_args(argv)

    lease = Lease(args.path, holder=args.holder, ttl=args.ttl)
    if args.action in ("acquire", "renew"):
        if lease.acquire():
            print(f"acquired {args.path} holder={lease.holder}")
            return 0
        cur = lease.current()
        # rc stays 1 for an unreadable lock too — it is "somebody else has it", not
        # a CLI error. planner_tick.sh treats any rc outside {0, 1} as a broken
        # helper and proceeds UNGUARDED, so widening the rc here would delete the
        # protection exactly when the lock file is in its most suspect state.
        print(f"held by {cur.holder}" if cur else "held by another (unreadable lock)")
        return 1
    if args.action == "release":
        lease.release()
        print(f"released {args.path}")
        return 0
    cur = lease.current()          # current
    print(f"{cur.holder} expires_at={cur.expires_at:.0f}" if cur else "free")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
