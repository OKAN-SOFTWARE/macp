"""Opt-in post-verify app screen check on a dedicated emulator node.

Runs in the orchestrator RUNTIME (the runner, OUTSIDE the ``claude -p`` verify
stage), mirroring :mod:`beta_publish`'s split: an app-repo task that asks for it
(``screen_check: true``) has its debug APK built, installed on the separate
on-demand emulator node ``macp-app-test`` via ``scripts/app-test-node``, and its
first screen pulled to ``Worker/runs/<run-id>/screens/*.png``. The node is driven
start -> shot -> **always stop**, and a one-line note ("화면 확인됨 / 불가(사유)")
is appended to the run's ``verify.md``. ``runner.StagedExecutor._app_screen_check``
calls :func:`run_check` the instant the verify stage finishes, gated on the
opt-in flag; a task with no flag is a no-op so unflagged tasks behave — and take
exactly as long — as before. That is the regression-safe contract.

Why the runner and not the verify stage: the node round-trip needs
gcloud/ssh/scp, which the verify stage's ``--allowed-tools`` whitelist forbids,
and it must run AFTER the ``claude -p`` subprocess exits so it is outside that
stage's timeout window. The note is appended to the bottom of ``verify.md`` after
the stage wrote it, so there is no file-ownership contention, and
``stages.parse_verify_verdict`` reads only the FIRST ``overall_status:`` match in
the top frontmatter — a bottom-appended note can never flip the verdict (and this
module never writes that token).

Always-stop: the node is a billed spot VM, so :func:`run_check` stops it on EVERY
exit path — build failure, shot failure, preemption, cap breach, or any
exception — mirroring ``runner._stop_build_daemons``'s never-raise two-tier
try/except. The stop call uses its own fixed timeout (``_STOP_TIMEOUT_S``), not
the possibly-expired cap deadline, so it still runs after a cap breach. A single
shared node is serialized by a :class:`lease.Lease` file lock; failing to take it
is a SKIP, never a code FAIL.

Testability: every external effect — the gradle build and the node start/shot/
stop — goes through the injected ``run=`` callable, so the unit suite passes a
fake and never shells out to gcloud/gradle/an emulator. Lock path, node script
and run dir are injected too, keeping tests inside ``tmp_path``. The real node
round-trip is therefore NOT exercised by this Implement pipeline (its allowed
tools carry no gcloud/ssh/scp); it is a runtime concern bounded by ``cap_s``.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from . import lease

# Default per-run node-uptime cap: 20 minutes, in seconds. A task overrides it
# with the ``screen_check_cap_min`` frontmatter key (minutes); the env seam
# ``MACP_SCREEN_CHECK_CAP_S`` wins over both and exists only so the unit suite can
# pin a tiny cap without a frontmatter round-trip.
_DEFAULT_CAP_S = 1200
_CAP_ENV = "MACP_SCREEN_CHECK_CAP_S"
# Lock TTL headroom over the cap: the holder is this process for the whole node
# round-trip, so the TTL is only the backstop for a holder that dies without
# releasing (the lease's same-host dead-PID reclaim clears it sooner).
_LOCK_MARGIN = 120
# The stop call gets its OWN fixed timeout, not the expired cap deadline, so the
# VM is still stopped after a cap breach. Stopping an already-stopped VM is
# idempotent, so stop is called unconditionally.
_STOP_TIMEOUT_S = 120
# The debug build task. Overridable per task with ``screen_check_gradle_task``
# (a repo whose debug APK is not produced by the default assembleDebug).
_DEFAULT_GRADLE_TASK = "assembleDebug"
# First screen only (install + launch + one screencap); UI-driven scenarios are
# deliberately out of scope for the first cut.

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class SkipCheck(Exception):
    """A handled reason to SKIP the screen check (build failed, no APK, node
    preemption, …). Carries the user-facing reason; :func:`run_check` turns it
    into a "화면 확인 불가(<reason>)" note and NEVER lets it escape — a screen
    check can only ever skip, it cannot fail the verify verdict."""


def _run(argv: list[str], *, cwd: str | None = None, env: dict | None = None,
         timeout: float | None = None) -> "subprocess.CompletedProcess[str]":
    """Default subprocess runner — the single seam every external effect flows
    through (the gradle build, the node start/shot/stop). Never raises on a
    non-zero exit (callers inspect ``returncode``); a timeout is allowed to
    propagate so the cap can turn it into a forced stop."""
    return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Opt-in gating + config (pure)
# ---------------------------------------------------------------------------

def requested(payload: dict) -> bool:
    """Did this task opt in to the screen check? Default OFF — only a truthy
    ``screen_check`` frontmatter value turns it on, so an unflagged task is a
    no-op. Mirrors ``runner._payload_auto_merge``'s string/bool handling (a
    frontmatter value can arrive as the string ``"true"``)."""
    v = (payload or {}).get("screen_check", False)
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(v)


def is_app_repo(repo: Path | str) -> bool:
    """A repo is an "app repo" (buildable to an APK) iff it has a ``gradlew``
    wrapper at its root. A vault task (no gradlew) is therefore never eligible,
    and a flagged non-app repo is skipped with a note rather than crashing."""
    return (Path(repo) / "gradlew").is_file()


def cap_seconds(payload: dict) -> int:
    """The per-run node-uptime cap in seconds.

    Priority: env ``MACP_SCREEN_CHECK_CAP_S`` (test seam) > frontmatter
    ``screen_check_cap_min`` (minutes) > ``_DEFAULT_CAP_S`` (20 min). Mirrors
    ``stages.stage_timeout_for``'s override parsing: a non-positive or
    unparseable value falls through to the next source rather than raising."""
    env_raw = os.environ.get(_CAP_ENV)
    if env_raw is not None:
        try:
            s = int(env_raw)
            if s > 0:
                return s
        except (TypeError, ValueError):
            pass
    raw = (payload or {}).get("screen_check_cap_min")
    if raw is not None:
        try:
            mins = int(raw)
            if mins > 0:
                return mins * 60
        except (TypeError, ValueError):
            pass
    return _DEFAULT_CAP_S


def gradle_task(payload: dict) -> str:
    """The gradle task that produces the debug APK, ``assembleDebug`` by default.
    Overridable per task (a repo whose debug variant is a different task)."""
    raw = (payload or {}).get("screen_check_gradle_task")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return _DEFAULT_GRADLE_TASK


# ---------------------------------------------------------------------------
# verify.md note (append-only; after the verify stage wrote the file)
# ---------------------------------------------------------------------------

def append_note(run_dir: Path | str, note: str,
                progress: Callable[[str], None] | None = None) -> None:
    """Append a one-block screen-check note to ``run_dir/verify.md``.

    Append mode, so the verify stage's content is untouched and the file is
    created if absent (defensive). The block never contains an ``overall_status:``
    token, and ``parse_verify_verdict`` reads only the first match in the top
    frontmatter anyway, so a bottom-appended note cannot change the verdict."""
    path = Path(run_dir) / "verify.md"
    block = f"\n## 실기 화면 확인 (screen_check)\n\n- {note}\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(block)
    if progress:
        progress(f"screen check: {note}")


# ---------------------------------------------------------------------------
# Node round-trip (always-stop, lock, cap) — every effect via injected run
# ---------------------------------------------------------------------------

def _remaining(deadline: float) -> float:
    """Seconds left until the cap deadline, floored at 1s so a call always has a
    positive timeout. A breached deadline yields the floor, so the next ``run``
    times out almost at once and the cap-breach path (forced stop) fires."""
    return max(1.0, deadline - time.monotonic())


def _node_env(run_dir: Path | str) -> dict:
    """Environment for the node script: inherit, but pin ``APP_TEST_OUT`` to the
    run's ``screens/`` dir so screenshots land straight in
    ``Worker/runs/<run-id>/screens/`` (AC #4). Node name/zone/gcloud binary are
    left to the script's own defaults — no literals in the engine module."""
    env = dict(os.environ)
    env["APP_TEST_OUT"] = str(Path(run_dir) / "screens")
    return env


def _node(run: Runner, node_script: Path | str, subcmd: str, *args: str,
          env: dict, timeout: float) -> "subprocess.CompletedProcess[str]":
    """One ``scripts/app-test-node <subcmd> [args…]`` invocation via the seam."""
    return run(["bash", str(node_script), subcmd, *args], env=env, timeout=timeout)


def _stop_node(run: Runner, node_script: Path | str, env: dict,
               progress: Callable[[str], None]) -> None:
    """``scripts/app-test-node stop`` behind a never-raise guard.

    The twin of ``runner._stop_build_daemons``: this runs in the inner
    ``finally`` of :func:`_run_locked`, so a fault here must NEVER replace the
    stage's real result or the exception the cap path re-raises. Its own fixed
    ``_STOP_TIMEOUT_S`` (not the expired cap deadline) means it still stops the VM
    after a cap breach; stopping an already-stopped VM is idempotent."""
    try:
        r = _node(run, node_script, "stop", env=env, timeout=_STOP_TIMEOUT_S)
        progress(f"screen check: node stop rc={getattr(r, 'returncode', None)}")
    except Exception as e:      # noqa: BLE001 — stop must not mask the real outcome
        progress(f"screen check: node stop skipped ({type(e).__name__})")


def _find_debug_apk(repo: Path | str) -> str | None:
    """First ``*debug*.apk`` under ``**/build/outputs/apk/**`` in the clone, or
    None. Sorted for determinism; a multi-module repo may hold several, and the
    first debug variant is good enough for a first-screen check (missing → a
    quiet SKIP, never a code FAIL)."""
    matches = sorted(Path(repo).glob("**/build/outputs/apk/**/*debug*.apk"))
    return str(matches[0]) if matches else None


def _build_debug_apk(repo: Path | str, task: str, run: Runner, deadline: float) -> str:
    """Build the debug APK in the worker checkout and return its path.

    rc != 0 -> ``SkipCheck("APK 빌드 실패")``; a build that produces no locatable
    debug APK -> ``SkipCheck("APK 산출물 없음")``. Both are SKIPs, not FAILs."""
    b = run(["./gradlew", task], cwd=str(repo), timeout=_remaining(deadline))
    if getattr(b, "returncode", 1) != 0:
        raise SkipCheck("APK 빌드 실패")
    apk = _find_debug_apk(repo)
    if apk is None:
        raise SkipCheck("APK 산출물 없음")
    return apk


def _shot_once(run: Runner, node_script: Path | str, apk: str, env: dict,
               deadline: float) -> str | None:
    """One ``shot`` attempt. Returns the pulled screenshot path (the script
    echoes ``dst`` on its last stdout line) or None when it fails / is silent."""
    r = _node(run, node_script, "shot", apk, env=env, timeout=_remaining(deadline))
    if getattr(r, "returncode", 1) != 0:
        return None
    lines = [ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _shot_with_retry(run: Runner, node_script: Path | str, apk: str, env: dict,
                     deadline: float, progress: Callable[[str], None]) -> str:
    """``shot`` with one preemption retry. The node was started by the caller; a
    first failure is treated as a possible spot preemption, so the node is
    ``start``ed again and ``shot`` retried EXACTLY once. Still failing ->
    ``SkipCheck`` (preemption/failure is a SKIP after one retry, AC #4)."""
    dst = _shot_once(run, node_script, apk, env, deadline)
    if dst is not None:
        return dst
    progress("screen check: shot failed — restarting node and retrying once")
    _node(run, node_script, "start", env=env, timeout=_remaining(deadline))
    dst = _shot_once(run, node_script, apk, env, deadline)
    if dst is not None:
        return dst
    raise SkipCheck("노드 선점/shot 실패 — 1회 재시도 후")


def _ok_note(dst: str, run_dir: Path | str) -> str:
    """Success note with the screenshot path shown relative to the run dir
    (``screens/<pkg>-<ts>.png``) when possible, else its basename."""
    try:
        shown = str(Path(dst).resolve().relative_to(Path(run_dir).resolve()))
    except (ValueError, OSError):
        shown = Path(dst).name
    return f"화면 확인됨 — {shown}"


def _run_locked(repo: Path | str, run_dir: Path | str, *, cap_s: int, task: str,
                node_script: Path | str, run: Runner,
                progress: Callable[[str], None]) -> str:
    """The node round-trip, assuming the lock is held. Returns a note; NEVER
    raises — every failure is absorbed into a "화면 확인 불가(…)" note.

    Structure: the whole body (build + start + shot) is the outer try; the inner
    ``finally`` stops the node UNCONDITIONALLY (AC #2 — even the APK-build-failure
    path stops, since stopping an un-started/stopped VM is idempotent)."""
    deadline = time.monotonic() + cap_s
    env = _node_env(run_dir)
    try:
        try:
            apk = _build_debug_apk(repo, task, run, deadline)
            _node(run, node_script, "start", env=env, timeout=_remaining(deadline))
            dst = _shot_with_retry(run, node_script, apk, env, deadline, progress)
            return _ok_note(dst, run_dir)
        finally:
            _stop_node(run, node_script, env, progress)
    except SkipCheck as e:
        return f"화면 확인 불가({e})"
    except subprocess.TimeoutExpired:
        return f"화면 확인 불가(상한 {cap_s // 60}분 초과 — 강제 stop)"
    except Exception as e:      # noqa: BLE001 — a screen check can only ever SKIP
        return f"화면 확인 불가(노드 오류: {type(e).__name__})"


def run_check(repo: Path | str, run_dir: Path | str, *, cap_s: int,
              lock_path: Path | str, node_script: Path | str,
              gradle_task: str = _DEFAULT_GRADLE_TASK, run: Runner = _run,
              progress: Callable[[str], None] | None = None) -> str:
    """Build the debug APK, capture the app's first screen on the shared emulator
    node, and return a one-line note for ``verify.md``. NEVER raises.

    A single shared node is serialized by a :class:`lease.Lease` file lock at
    ``lock_path``; failing to take it means another run holds the node, so this
    returns a SKIP note with ZERO node calls (AC #3). On success the node is
    driven start -> shot(-> retry once) and always stopped (AC #2); the screenshot
    lands under ``run_dir/screens/`` (AC #4). The whole node round-trip is bounded
    by ``cap_s`` and forcibly stopped past it (AC #5)."""
    progress = progress or (lambda _m: None)
    lock = lease.Lease(str(lock_path), ttl=cap_s + _LOCK_MARGIN)
    if not lock.acquire():
        return "화면 확인 불가(노드 사용 중 — 락 획득 실패)"
    try:
        return _run_locked(repo, run_dir, cap_s=cap_s, task=gradle_task,
                           node_script=node_script, run=run, progress=progress)
    finally:
        lock.release()
