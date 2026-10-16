"""GitMailbox git calls must be wall-clock bounded.

Why this file exists
--------------------
``GitMailbox._git`` ran ``subprocess.run`` with no timeout. ``receive()`` pulls
first and ``send()`` commits + pushes, so a wedged remote or a stalled credential
helper made the call — and the orchestrator tick that made it — never return.
A timeout now comes back as ``CompletedProcess(returncode=124)``, so the existing
"pull failed → use local state" / "push failed → retry later" paths run instead.

How the hang is simulated without a network
-------------------------------------------
Same approach as ``test_worker_git_timeout.py``: a shell-script ``git`` shim is
prepended to ``PATH``. It sleeps (then exits 0) or fails for the subcommands under
test and ``exec``s the real git for everything else. Unlike that file's shim, this
one skips a leading ``-c <val>`` pair, because the mailbox pulls with
``git -c rebase.autoStash=true pull …`` and ``$1`` there is ``-c``.

The discriminating axis is **wall clock** (and, for push, the attempt count): a
shim that sleeps and then exits 0 makes the unbounded code return late rather
than raise, which a return-value-only assertion cannot see.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import mailbox
from todo_system.orchestrator.message import PING, Envelope

# How long the shim sleeps on a hanging subcommand. Must comfortably exceed the
# 1s ceilings the tests patch in, so the timeout always fires first.
_SHIM_SLEEP_S = 5

_AGENT = "probe"


def _init_repo(path: Path) -> None:
    """A real git repo with one commit on `main` and an unreachable `origin`."""
    path.mkdir(parents=True, exist_ok=True)
    steps = [
        ["git", "init", "-q", "-b", "main", str(path)],
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(path), "config", "user.name", "test"],
        ["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "root"],
        # `origin` must exist so `_has_origin()` is true and pull/push are the
        # calls under test; it is never reached because the shim intercepts them.
        ["git", "-C", str(path), "remote", "add", "origin",
         "https://example.invalid/mailbox.git"],
    ]
    for args in steps:
        r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{args} failed: {r.stderr}"


def _install_git_shim(tmp_path: Path, monkeypatch, *,
                      hang: tuple[str, ...] = (),
                      fail: tuple[str, ...] = ()) -> Path:
    """Prepend a ``git`` that fakes hangs/failures per subcommand, real otherwise.

    ``hang`` sleeps ``_SHIM_SLEEP_S`` and then exits **0**; ``fail`` exits 1 at
    once (an ordinary offline failure, no DNS latency). The subcommand is ``$1``,
    or ``$3`` when the call opens with a ``-c <val>`` pair.

    Callers that need a genuine repo must build it *before* calling this.
    """
    real_git = shutil.which("git")
    assert real_git, "the real git must be on PATH for the shim to delegate to it"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "git"
    cases = ""
    if hang:
        cases += (f"  {'|'.join(hang)})\n"
                  f"    sleep {_SHIM_SLEEP_S}\n"
                  "    exit 0\n"
                  "    ;;\n")
    if fail:
        cases += (f"  {'|'.join(fail)})\n"
                  '    echo "shim: simulated failure" >&2\n'
                  "    exit 1\n"
                  "    ;;\n")
    shim.write_text(
        "#!/bin/sh\n"
        'sub="$1"\n'
        'if [ "$1" = "-c" ]; then sub="$3"; fi\n'
        'case "$sub" in\n'
        f"{cases}"
        "esac\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return shim


def _mailbox(tmp_path: Path) -> mailbox.GitMailbox:
    """A GitMailbox rooted in a tmp repo — never the live vault.

    The root is created first so `_toplevel` resolves inside the tmp repo; the
    assert is the safety net against running git against a real checkout.
    """
    repo = tmp_path / "vault"
    _init_repo(repo)
    root = repo / "Worker" / "agents"
    root.mkdir(parents=True)
    mb = mailbox.GitMailbox(root)
    assert mb.repo_dir.resolve().is_relative_to(tmp_path.resolve()), (
        f"repo_dir escaped tmp_path ({mb.repo_dir}) — "
        "refusing to run git against a real checkout")
    return mb


def _shrink(monkeypatch, name: str) -> None:
    # hasattr first, then raising=False: the assert catches a renamed constant,
    # while raising=False lets a RED control run the unbounded code instead of
    # erroring out at patch time.
    assert hasattr(mailbox, name)
    monkeypatch.setattr(mailbox, name, 1, raising=False)


def _count_pushes(mb: mailbox.GitMailbox, monkeypatch) -> list[tuple[str, ...]]:
    """Record every `push` that goes through `mb._git`."""
    pushes: list[tuple[str, ...]] = []
    real = mb._git

    def spy(*args, **kw):
        if "push" in args:
            pushes.append(args)
        return real(*args, **kw)

    monkeypatch.setattr(mb, "_git", spy)
    return pushes


def test_git_timeout_grades_pull_with_c_pair_as_network(monkeypatch):
    """The `-c rebase.autoStash=true` pair must not hide `pull` from the grader."""
    pull = ("-c", "rebase.autoStash=true", "pull", "--rebase", "origin", "main")
    assert mailbox._git_timeout(pull) == mailbox.GIT_TIMEOUT_NETWORK_S
    assert mailbox._git_timeout(("push", "origin", "main")) == mailbox.GIT_TIMEOUT_NETWORK_S
    for local in (("remote",), ("add", "x"), ("rebase", "--abort"),
                  ("diff", "--cached", "--quiet")):
        assert mailbox._git_timeout(local) == mailbox.GIT_TIMEOUT_LOCAL_S, local
    assert mailbox.GIT_TIMEOUT_NETWORK_S > mailbox.GIT_TIMEOUT_LOCAL_S

    # Read at call time, not baked into a default argument.
    monkeypatch.setattr(mailbox, "GIT_TIMEOUT_NETWORK_S", 7)
    assert mailbox._git_timeout(pull) == 7


def test_git_timeout_becomes_rc_124(tmp_path, monkeypatch):
    """A hung pull is a return value, not a raised TimeoutExpired — and it is fast."""
    mb = _mailbox(tmp_path)
    _install_git_shim(tmp_path, monkeypatch, hang=("pull",))
    _shrink(monkeypatch, "GIT_TIMEOUT_NETWORK_S")

    started = time.monotonic()
    r = mb._git("-c", "rebase.autoStash=true", "pull", "--rebase", "origin", "main")
    elapsed = time.monotonic() - started

    assert r.returncode == mailbox.GIT_TIMEOUT_RC == 124
    assert "timeout" in r.stderr
    assert "pull" in r.stderr
    assert elapsed < 4.0, f"_git took {elapsed:.1f}s — the ceiling did not fire"


def test_timeout_normalizes_none_and_bytes_streams(tmp_path):
    """`TimeoutExpired.stdout`/`.stderr` may be None or bytes; callers need str."""
    mb = _mailbox(tmp_path)
    exc = subprocess.TimeoutExpired(cmd=["git", "push"], timeout=1,
                                    output=b"partial", stderr=None)
    with mock.patch.object(mailbox.subprocess, "run", side_effect=exc):
        r = mb._git("push", "origin", "main")
    assert r.returncode == mailbox.GIT_TIMEOUT_RC
    assert isinstance(r.stdout, str) and r.stdout == "partial"
    assert isinstance(r.stderr, str) and "timeout" in r.stderr


def test_receive_survives_hung_pull(tmp_path, monkeypatch):
    """The tick path: a hung pull must not wedge receive(); local state is used."""
    mb = _mailbox(tmp_path)
    sent = mailbox.FileMailbox(mb.root).send(Envelope(to=_AGENT, type=PING))
    _install_git_shim(tmp_path, monkeypatch, hang=("pull",))
    _shrink(monkeypatch, "GIT_TIMEOUT_NETWORK_S")

    started = time.monotonic()
    msgs = mb.receive(_AGENT)
    elapsed = time.monotonic() - started

    assert [m.msg_id for m in msgs] == [sent.stem]
    assert elapsed < 4.0, f"receive took {elapsed:.1f}s — the pull was not bounded"


def test_send_survives_hung_push(tmp_path, monkeypatch):
    """A hung push returns at once: one attempt, no 3x(push+pull) retry burn."""
    mb = _mailbox(tmp_path)
    # `pull` fails instantly so the wall clock measures only the push ceiling.
    _install_git_shim(tmp_path, monkeypatch, hang=("push",), fail=("pull",))
    _shrink(monkeypatch, "GIT_TIMEOUT_NETWORK_S")
    pushes = _count_pushes(mb, monkeypatch)

    started = time.monotonic()
    path = mb.send(Envelope(to=_AGENT, type=PING))
    elapsed = time.monotonic() - started

    assert path.exists()
    assert len(pushes) == 1, f"a timed-out push was retried: {pushes}"
    assert elapsed < 4.0, f"send took {elapsed:.1f}s — the push was not bounded"


def test_plain_push_failure_still_retries(tmp_path, monkeypatch):
    """Preservation: only the timeout rc short-circuits; a rejected push retries 3x."""
    mb = _mailbox(tmp_path)
    _install_git_shim(tmp_path, monkeypatch, fail=("push", "pull"))
    pushes = _count_pushes(mb, monkeypatch)

    path = mb.send(Envelope(to=_AGENT, type=PING))

    assert path.exists()
    assert len(pushes) == 3, f"expected the 3-attempt rebase-retry loop, got {pushes}"


def test_toplevel_timeout_falls_back_to_root(tmp_path, monkeypatch):
    """A wedged `rev-parse` in the constructor falls back to the root, fast."""
    root = tmp_path / "agents"
    root.mkdir()
    _install_git_shim(tmp_path, monkeypatch, hang=("rev-parse",))
    _shrink(monkeypatch, "GIT_TIMEOUT_LOCAL_S")

    started = time.monotonic()
    top = mailbox.GitMailbox._toplevel(root)
    elapsed = time.monotonic() - started

    assert top == root
    assert elapsed < 4.0, f"_toplevel took {elapsed:.1f}s — the ceiling did not fire"


def test_every_subprocess_run_in_gitmailbox_is_bounded():
    """Structural regression: no git call in GitMailbox may be unbounded.

    Written against the source so a git call added later — one no behavioural
    test here covers — still trips the gate.
    """
    tree = ast.parse(Path(mailbox.__file__).read_text(encoding="utf-8"))
    cls = next((n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "GitMailbox"), None)
    assert cls is not None, "GitMailbox not found — was it renamed?"

    calls = [n for n in ast.walk(cls)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "run" and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "subprocess"]
    # Vacuity guard: `_git` and `_toplevel` each make one.
    assert len(calls) >= 2, f"expected GitMailbox's subprocess.run calls, got {len(calls)}"

    unbounded = [ast.unparse(c)[:80] for c in calls
                 if not any(kw.arg == "timeout" for kw in c.keywords)]
    assert unbounded == [], f"git calls without a timeout: {unbounded}"
