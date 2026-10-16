"""Detached long jobs — work that outlives the stage that starts it.

Some tasks are gated on a process that simply takes longer than a stage: a
decade of daily candles for 100 tickers is ~2,500 paginated API calls, ~75
minutes. Nothing in the pipeline could hold that. A `claude -p` stage is a
one-shot subprocess with a 60-minute cap, and anything it backgrounds dies with
it — ai-stock-trading-8b lost its collection at 15/100 tickers exactly when the
implement stage exited, having written "I'll pick up when the collection exits"
(no stage is ever re-invoked; the same one-process fallacy that killed a verify
stage the same day).

So the job is started OUTSIDE the stage tree and merely observed:

    start()  -> setsid'd process, its own session (and its own systemd scope
                when run from a unit), survives the runner itself
    poll()   -> running | done | failed | absent, from state on disk

The task defers — cheaply, without spawning an agent — until the job exits, and
only then runs its pipeline against complete data. Downstream `depends_on` tasks
need no new machinery: the upstream simply isn't done yet.

Exit status is captured by the wrapper shell rather than waitpid, because the
process that started the job is long gone by the time anyone asks. `rc` file
present = finished (and how); absent + live pid = running; absent + dead pid =
killed. Stdlib-only, and every failure is a state, never an exception.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import datetime as dt
from pathlib import Path

from .. import paths

# Vault-data root. start= is paths.vault_search_start(): the engine root in a
# checkout (byte-identical to the former parents[2] anchor), None in an
# installed wheel so the cwd-upward search runs instead of a site-packages anchor.
REPO_ROOT = paths.vault_root(start=paths.vault_search_start())
JOBS_DIR = REPO_ROOT / "Worker" / "jobs"


def _state_path(slug: str) -> Path:
    return JOBS_DIR / f"{slug}.json"


def _rc_path(slug: str) -> Path:
    return JOBS_DIR / f"{slug}.rc"


def log_path(slug: str) -> Path:
    return JOBS_DIR / f"{slug}.log"


def read(slug: str) -> dict | None:
    try:
        return json.loads(_state_path(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _signalable(pid: object) -> int | None:
    """The pid if it names ONE real process, else None. The single gate in front
    of every os.kill in this module.

    POSIX overloads kill's first argument: 0 is "my whole process group", -1 is
    "every process this uid may signal", and anything below -1 is a process
    group. `start()` records -1 whenever it cannot read the pid file, so that
    value is reachable by ordinary means — and on 2026-09-10 it reached `stop()`
    from a test teardown and SIGTERM'd the entire login session: the GUI agents,
    the daemons, the remote-control server, and the editor running the suite. A
    pid we did not successfully spawn is not something to signal, so it is
    refused here once rather than at each call site.
    """
    try:
        n = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _alive(pid: object) -> bool:
    """Signal 0 probes without delivering. Not-our-child is fine — we only ever
    ask about a session leader we spawned, and a PID that has been recycled at
    worst delays a retry by one poll.

    A non-positive pid answers False instead of probing: `kill(-1, 0)` asks "may
    I signal ANY of my processes", which is always yes, so a job that never
    launched would otherwise read as running forever."""
    real = _signalable(pid)
    if real is None:
        return False
    try:
        os.kill(real, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _pid_path(slug: str) -> Path:
    return JOBS_DIR / f"{slug}.pid"


def _scope_prefix() -> list[str]:
    """argv that moves the job into its own transient systemd scope, or [].

    A new session is not enough under systemd. The runner is a unit with
    KillMode=control-group, and when it exits to pick up new code systemd kills
    every process in its cgroup, including our orphaned job. On 2026-10-08 two
    merged worker PRs killed an ai-stock-trading sweep twice in 75 minutes, both
    times as "killed without an exit code". A scope is a separate cgroup, so
    stopping the runner leaves the job running.

    INVOCATION_ID is only set inside a systemd unit, which is the only place
    the cgroup kill happens. Launchd on the Mac and a plain shell get [] and
    behave as before.
    """
    if not os.environ.get("INVOCATION_ID"):
        return []
    exe = shutil.which("systemd-run")
    if not exe:
        return []
    return [exe, "--user", "--scope", "--quiet", "--collect"]


def _launch(argv: list[str], cwd: str | Path | None) -> None:
    subprocess.run(argv, cwd=str(cwd) if cwd else None,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, start_new_session=True, timeout=30)


def start(slug: str, cmd: str, cwd: str | Path | None = None) -> dict:
    """Launch `cmd` detached and record it. Returns the state dict.

    Two details that look fussy and are not:

    `( cmd )` runs in a SUBSHELL so the exit-code write survives. Written flat as
    `cmd; printf ... > rc`, any job ending in `exit N` — or any script that calls
    exit — terminates the wrapper before it records anything, and the job then
    polls as "killed" forever.

    The job is then backgrounded and ORPHANED: the shell we spawn writes the real
    pid and exits immediately, so our own child is reaped at once instead of
    lingering as a zombie. That matters because the runner is long-lived and
    `kill(pid, 0)` succeeds against a zombie — a finished job would read as still
    running, forever. Orphaning also means the job is reparented to init and
    survives the runner being restarted, which is the point: deploying a fix must
    not kill a 75-minute collection that is 60 minutes in. Under systemd that
    also needs a separate scope (see _scope_prefix).
    """
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    rc, log, pidf = _rc_path(slug), log_path(slug), _pid_path(slug)
    for p_ in (rc, pidf):
        p_.unlink(missing_ok=True)
    wrapped = (f"( ( {cmd} ) > {log} 2>&1; printf %s $? > {rc} ) & "
               f"printf %s $! > {pidf}")
    # Stamped BEFORE the launch, on purpose. The job is backgrounded, so it can
    # already be writing files by the time this call returns; a stamp taken
    # afterwards can land AFTER the job's first output and — since it is floored
    # to the second — read as newer than a file the job itself just wrote. That
    # timestamp is what stages.snapshot_job_artifacts uses to tell this job's
    # output from another task's leftovers, and an over-late stamp silently
    # excludes real data. Early is the safe direction: it can only widen the
    # window by under a second, and the untracked + job-cwd conditions still bound it.
    started_ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    sh = ["/bin/sh", "-c", wrapped]
    prefix = _scope_prefix()
    if prefix:
        try:
            _launch(prefix + sh, cwd)
        except (OSError, subprocess.SubprocessError):
            pass
    # No pid file means the job never started. That covers no systemd, and a
    # systemd-run that could not reach the user bus. A plain launch is still
    # better than no job.
    if not pidf.exists():
        _launch(sh, cwd)
    try:
        pid = int(pidf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = -1
    state = {"slug": slug, "pid": pid, "cmd": cmd,
             "cwd": str(cwd) if cwd else None,
             "started_ts": started_ts}
    _state_path(slug).write_text(json.dumps(state, indent=1), encoding="utf-8")
    return state


def poll(slug: str) -> dict:
    """{state: absent|running|done|failed, ...}. Never raises."""
    st = read(slug)
    if not st:
        return {"state": "absent"}
    out = dict(st)
    out["elapsed_s"] = _elapsed(st.get("started_ts"))
    rc_file = _rc_path(slug)
    if rc_file.exists():
        try:
            rc = int((rc_file.read_text(encoding="utf-8") or "1").strip() or 1)
        except ValueError:
            rc = 1
        out["rc"] = rc
        out["state"] = "done" if rc == 0 else "failed"
        return out
    if _alive(st.get("pid")):
        out["state"] = "running"
        return out
    # No exit code and no process: the job was killed (reboot, OOM, someone's
    # kill -9). Report failed rather than done — silently treating a truncated
    # collection as complete is the exact bug this module exists to prevent.
    out["state"] = "failed"
    out["rc"] = None
    return out


def _elapsed(started: str | None) -> int:
    try:
        t = dt.datetime.fromisoformat(str(started))
        return int((dt.datetime.now().astimezone() - t).total_seconds())
    except (TypeError, ValueError):
        return 0


def clear(slug: str) -> None:
    """Forget a job so the next dispatch starts a fresh one."""
    for p in (_state_path(slug), _rc_path(slug), _pid_path(slug)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def stop(slug: str) -> bool:
    """Terminate a running job (whole session, so children go too)."""
    st = read(slug)
    if not st:
        return False
    pid = _signalable(st.get("pid"))
    if pid is None:
        # -1 is exactly what start() writes when the job never launched. Handing
        # that to os.kill does not miss — it signals every process this user
        # owns. Nothing to stop is a False, not a session wipe.
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ProcessLookupError, ValueError, KeyError):
        return False
