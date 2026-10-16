"""Branch preparation must be wall-clock bounded — external repo *and* vault.

Why this file exists
--------------------
On 2026-09-25 the runner's cgroup went over ``memory.high`` and ``git-remote-https``
sat inside a git call for 18~72 minutes. Nothing in ``ensure_external_repo_ready``
bounded a subprocess, so three runner slots stayed occupied by threads that would
never return while the dashboard showed idle.

``ensure_vault_branch_fresh`` — the same preparation step for a **vault** task —
was left unbounded by that fix and is covered here too (2026-09-26). Its ceilings
are graded the same way, but its ``pull`` timeout is **fatal** where the external
repo's is a warning: the vault is the tick's own live checkout, so branching off a
stale ``main`` is contamination, whereas an external checkout is a throwaway cache.

How the hang is simulated without a network
-------------------------------------------
A shell-script ``git`` shim is prepended to ``PATH``. It sleeps (then exits 0) for
the subcommands under test and ``exec``s the **real** git for everything else, so
the local half of the function keeps working against a genuine repo and only the
targeted calls hang. ``subprocess.run(["git", ...])`` resolves ``git`` via
``execvp`` in the child, so no injection seam in the function is needed.

The discriminating axis is **wall clock**, not just the return value: a shim that
merely sleeps and then exits 0 makes the *unbounded* code return late rather than
raise, which a return-value-only assertion cannot see (learned in
``Memory/episodes/2026-09-26-worker-macp-runner-git-timeout.md``). For the vault
path the *message* is a second discriminating axis, because a wedged local call
can already produce ``ok=False`` for the wrong reason (a timed-out ``rev-parse``
read as "branch exists" fails later in ``branch -D``).

Neither function's signature is extended with an injection keyword — a test
elsewhere that patches the whole function would break on an unexpected argument.
``ensure_vault_branch_fresh`` hard-codes ``cwd=ROOT``, so the vault tests must
repoint the module global ``worker.ROOT`` at a tmp repo; ``_vault_repo`` does that
behind a safety assert, since an unpatched run would drive ``git checkout main`` /
``branch -D`` against the real vault checkout.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from todo_system import worker

# Subcommands the shim pretends to hang on — the remote-talking calls in
# ensure_external_repo_ready: clone / fetch / remote set-head / pull.
_HANG_SUBCOMMANDS = ("clone", "fetch", "pull", "remote")

# How long the shim sleeps on a hanging subcommand. Must comfortably exceed the
# 1s ceilings the tests patch in, so the timeout always fires first.
_SHIM_SLEEP_S = 5


def _install_git_shim(tmp_path: Path, monkeypatch, *,
                      hang: tuple[str, ...] = (),
                      fail: tuple[str, ...] = ()) -> Path:
    """Prepend a ``git`` that fakes hangs/failures per subcommand, real otherwise.

    ``hang`` sleeps ``_SHIM_SLEEP_S`` and then exits **0** — the shape that makes
    an *unbounded* caller return late rather than raise. ``fail`` exits 1 at once,
    which is what an ordinary offline failure looks like. Having both lets a test
    wedge one local call without paying real DNS latency on the network one.

    Dispatch is on ``$1``, so naming a subcommand covers every flag spelling of
    that call — note ``branch`` therefore matches both ``branch --show-current``
    and ``branch -D``, which is why the vault tests wedge ``rev-parse`` instead.

    Callers that need a genuine repo must build it *before* calling this, while
    ``git`` still means git.
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
        "case \"$1\" in\n"
        f"{cases}"
        "esac\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return shim


def _install_slow_git(tmp_path: Path, monkeypatch) -> Path:
    """The external-repo shim: hangs on every remote-talking subcommand."""
    return _install_git_shim(tmp_path, monkeypatch, hang=_HANG_SUBCOMMANDS)


@pytest.fixture
def slow_git(tmp_path, monkeypatch) -> Path:
    return _install_slow_git(tmp_path, monkeypatch)


def _make_task(tmp_path: Path, checkout: Path) -> worker.WorkerTask:
    """A WorkerTask whose repo/working_dir are frontmatter-derived properties."""
    task = worker.WorkerTask(
        slug="fake-ext",
        path=tmp_path / "fake-ext.md",
        rel_path="Todo/items/fake-ext.md",
        frontmatter={
            "repo": "https://example.invalid/fake-ext.git",
            "working_dir": str(checkout),
        },
    )
    # Safety net: an unpatched run must never reach a real checkout.
    resolved = worker.resolve_working_dir(task)
    assert resolved.is_relative_to(tmp_path), f"working_dir escaped tmp_path: {resolved}"
    return task


def _init_repo(path: Path) -> None:
    """A real git repo with one commit on `main` and an unreachable `origin`."""
    path.mkdir(parents=True, exist_ok=True)
    steps = [
        ["git", "init", "-q", "-b", "main", str(path)],
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        ["git", "-C", str(path), "config", "user.name", "test"],
        ["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "root"],
        # `origin` must exist for `fetch origin` / `pull origin main` to be the
        # calls under test; it is never reached because the shim intercepts them.
        ["git", "-C", str(path), "remote", "add", "origin",
         "https://example.invalid/fake-ext.git"],
    ]
    for args in steps:
        r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{args} failed: {r.stderr}"


def _vault_repo(tmp_path: Path, monkeypatch) -> Path:
    """A tmp repo standing in for the live vault, with ``worker.ROOT`` repointed.

    ``ensure_vault_branch_fresh`` hard-codes ``cwd=ROOT`` and takes no injection
    keyword, so repointing the module global is the only seam. The assert is the
    safety net: if the patch ever stops taking (renamed global, module reload)
    the test stops here rather than running ``git checkout main`` / ``branch -D``
    against the real vault checkout.
    """
    repo = tmp_path / "vault"
    _init_repo(repo)
    monkeypatch.setattr(worker, "ROOT", repo)
    assert worker.ROOT.is_relative_to(tmp_path), (
        f"worker.ROOT patch did not take ({worker.ROOT}) — "
        "refusing to run git against the live vault")
    return repo


def _local_branches(repo: Path) -> set[str]:
    """Local branch names read straight off ``.git`` — deliberately no git call.

    The vault tests have a ``git`` shim on ``PATH``, so asking git would be
    asking the thing under test. Reading refs covers both loose and packed.
    """
    names: set[str] = set()
    heads = repo / ".git" / "refs" / "heads"
    if heads.is_dir():
        names |= {str(p.relative_to(heads)) for p in heads.rglob("*") if p.is_file()}
    packed = repo / ".git" / "packed-refs"
    if packed.exists():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if " refs/heads/" in line:
                names.add(line.split(" refs/heads/", 1)[1].strip())
    return names


def test_prepare_git_timeout_becomes_rc_124(slow_git):
    """A hung git is a return value, not a raised TimeoutExpired."""
    result = worker._prepare_git(["git", "fetch", "origin"], timeout=0.5)
    assert result.returncode == worker.GIT_TIMEOUT_RC == 124
    assert "timeout" in result.stderr
    assert "git fetch origin" in result.stderr


def test_prepare_git_normalizes_none_and_bytes_streams():
    """`TimeoutExpired.stdout`/`.stderr` may be None or bytes; callers need str."""
    exc = subprocess.TimeoutExpired(cmd=["git", "pull"], timeout=1,
                                    output=b"partial out", stderr=None)
    with mock.patch.object(worker.subprocess, "run", side_effect=exc):
        result = worker._prepare_git(["git", "pull"], timeout=1)
    assert result.returncode == worker.GIT_TIMEOUT_RC
    assert isinstance(result.stdout, str) and result.stdout == "partial out"
    assert isinstance(result.stderr, str) and "timeout" in result.stderr


def test_clone_timeout_is_explicit_failure_and_returns_fast(tmp_path, slow_git,
                                                            monkeypatch):
    """The clone path — the one that actually wedged — fails explicitly and fast.

    This is the primary discriminating test. Before the fix the clone was
    unbounded, so the shim's sleep ran to completion and exited 0: the function
    walked on past a checkout that was never created and came back far later with
    a message that says nothing about a timeout.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()  # exists but has no .git → the clone branch is taken
    task = _make_task(tmp_path, checkout)

    # hasattr first, then raising=False: the assert catches a renamed constant,
    # while raising=False lets the RED control actually run the unbounded code
    # instead of erroring out at patch time.
    assert hasattr(worker, "GIT_TIMEOUT_CLONE_S")
    monkeypatch.setattr(worker, "GIT_TIMEOUT_CLONE_S", 1, raising=False)

    started = time.monotonic()
    ok, msg, wd = worker.ensure_external_repo_ready(task)
    elapsed = time.monotonic() - started

    assert ok is False
    assert "timeout" in msg, f"timeout must be distinguishable from 'clone failed': {msg}"
    assert wd == checkout
    assert elapsed < 10.0, f"prepare_branch took {elapsed:.1f}s — the ceiling did not fire"


def test_hung_network_calls_stay_non_fatal_and_bounded(tmp_path, monkeypatch, capsys):
    """fetch / set-head / pull keep their non-fatal (offline) contract, bounded.

    Timing out on these three must NOT become a task failure — the function was
    deliberately written to carry on with a stale tree when offline, and a wedged
    remote is one flavour of offline. What changes is only that it returns.
    """
    checkout = tmp_path / "checkout"
    _init_repo(checkout)             # real repo first, while `git` still means git
    _install_slow_git(tmp_path, monkeypatch)
    task = _make_task(tmp_path, checkout)

    for name in ("GIT_TIMEOUT_FETCH_S", "GIT_TIMEOUT_SETHEAD_S", "GIT_TIMEOUT_PULL_S"):
        assert hasattr(worker, name)
        monkeypatch.setattr(worker, name, 1, raising=False)

    started = time.monotonic()
    ok, msg, wd = worker.ensure_external_repo_ready(task)
    elapsed = time.monotonic() - started

    assert ok is True, f"a hung remote must stay non-fatal, got: {msg}"
    assert wd == checkout
    assert elapsed < 20.0, f"prepare_branch took {elapsed:.1f}s — ceilings did not fire"

    out = capsys.readouterr().out
    for expected in ("fetch timeout after", "remote set-head timeout after",
                     "pull main timeout after"):
        assert expected in out, f"missing warn {expected!r} in:\n{out}"


def test_vault_pull_timeout_is_explicit_failure_and_returns_fast(tmp_path, monkeypatch):
    """The vault's network call: a hung pull fails loudly instead of branching.

    This is the primary discriminating test for the vault path. Before the fix the
    pull was unbounded, so the shim's sleep ran to completion and exited 0: the
    function walked on and cut ``worker/probe`` from whatever stale ``main`` the
    checkout happened to hold, returning ``ok=True`` far later. Unlike the
    external-repo path this must be **fatal** — the vault is the tick's own live
    checkout, so a stale base is contamination, not a throwaway cache.
    """
    repo = _vault_repo(tmp_path, monkeypatch)
    _install_git_shim(tmp_path, monkeypatch, hang=("pull",))

    # hasattr first, then raising=False: the assert catches a renamed constant,
    # while raising=False lets the RED control actually run the unbounded code
    # instead of erroring out at patch time.
    assert hasattr(worker, "GIT_TIMEOUT_PULL_S")
    monkeypatch.setattr(worker, "GIT_TIMEOUT_PULL_S", 1, raising=False)

    started = time.monotonic()
    ok, msg = worker.ensure_vault_branch_fresh("worker/probe")
    elapsed = time.monotonic() - started

    assert ok is False, f"a hung pull must not silently branch off a stale main: {msg}"
    assert "timed out" in msg or "timeout" in msg, \
        f"timeout must be distinguishable from 'pull main failed (offline?)': {msg}"
    assert elapsed < 10.0, f"vault branch prep took {elapsed:.1f}s — the ceiling did not fire"
    assert "worker/probe" not in _local_branches(repo), \
        "the branch was created anyway — the failure return did not stop the function"


def test_vault_pull_plain_failure_stays_non_fatal(tmp_path, monkeypatch, capsys):
    """Preservation: an ordinary (offline) pull failure still warns and carries on.

    Only the *wedged* case changes. Making the whole pull fatal would kill every
    vault task on a host that cannot reach origin, so this pins the ``elif``
    ordering — rc=124 fatal, any other non-zero rc a warning.

    Green both before and after the fix by construction; it is here to stop the
    fix from over-reaching, not to demonstrate it.
    """
    repo = _vault_repo(tmp_path, monkeypatch)
    _install_git_shim(tmp_path, monkeypatch, fail=("pull",))

    ok, msg = worker.ensure_vault_branch_fresh("worker/probe")

    assert ok is True, f"a plain offline pull failure must stay non-fatal, got: {msg}"
    assert "worker/probe" in _local_branches(repo), \
        f"the fresh branch was not created: {msg}"
    assert "pull main failed (offline?)" in capsys.readouterr().out


def test_vault_local_git_timeout_is_explicit_failure(tmp_path, monkeypatch):
    """A wedged local call must be told apart from its ordinary non-zero rc.

    ``git rev-parse --verify --quiet`` answers rc=1 for a missing branch, which is
    the normal first-run case — so ``rc != 0`` may not be treated as failure and
    the timeout can only be recognised by matching ``GIT_TIMEOUT_RC`` exactly.
    Before the fix a wedged ``rev-parse`` was read as "branch exists" and surfaced
    as an unrelated ``branch delete failed`` message several seconds later.
    """
    repo = _vault_repo(tmp_path, monkeypatch)
    # `pull` fails instantly so this test measures only the local hang; letting
    # the real pull reach example.invalid would add DNS latency to the wall clock.
    _install_git_shim(tmp_path, monkeypatch, hang=("rev-parse",), fail=("pull",))

    assert hasattr(worker, "GIT_TIMEOUT_LOCAL_S")
    monkeypatch.setattr(worker, "GIT_TIMEOUT_LOCAL_S", 1, raising=False)

    started = time.monotonic()
    ok, msg = worker.ensure_vault_branch_fresh("worker/probe")
    elapsed = time.monotonic() - started

    assert ok is False, f"a wedged rev-parse must not be read as 'branch absent': {msg}"
    assert "timed out" in msg or "timeout" in msg, \
        f"timeout must be distinguishable from 'branch delete failed': {msg}"
    assert elapsed < 10.0, f"vault branch prep took {elapsed:.1f}s — the ceiling did not fire"
    assert "worker/probe" not in _local_branches(repo)


def _bounded_git_calls(func: ast.FunctionDef) -> list[ast.Call]:
    """Every `subprocess.run(...)` / `_prepare_git(...)` call inside `func`."""
    calls: list[ast.Call] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        is_subprocess_run = (isinstance(f, ast.Attribute) and f.attr == "run"
                             and isinstance(f.value, ast.Name)
                             and f.value.id == "subprocess")
        is_wrapper = isinstance(f, ast.Name) and f.id == "_prepare_git"
        if is_subprocess_run or is_wrapper:
            calls.append(node)
    return calls


def _first_arg_words(call: ast.Call) -> tuple[str, ...]:
    """The literal string prefix of the argv list, e.g. ("git", "remote", "set-head")."""
    if not call.args or not isinstance(call.args[0], ast.List):
        return ()
    words: list[str] = []
    for el in call.args[0].elts:
        if isinstance(el, ast.Constant) and isinstance(el.value, str):
            words.append(el.value)
        else:
            break
    return tuple(words)


def test_every_git_call_in_prepare_branch_is_bounded():
    """Structural regression: no git call in the function may be unbounded.

    Written against the source rather than behaviour so that a *fifth* git call
    added later — one no behavioural test here covers — still trips the gate.
    """
    tree = ast.parse(Path(worker.__file__).read_text(encoding="utf-8"))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "ensure_external_repo_ready"), None)
    assert func is not None, "ensure_external_repo_ready not found — was it renamed?"

    calls = _bounded_git_calls(func)
    # Vacuity guard: if the wrapper were renamed, `calls` would silently be empty
    # and every assertion below would pass for free.
    assert len(calls) >= 4, f"expected the function's git calls to be found, got {len(calls)}"

    unbounded = [ast.unparse(c)[:80] for c in calls
                 if not any(kw.arg == "timeout" for kw in c.keywords)]
    assert unbounded == [], f"git calls without a timeout: {unbounded}"

    # The four network calls named in the acceptance criteria, specifically.
    prefixes = [_first_arg_words(c) for c in calls]
    for required in (("git", "clone"), ("git", "fetch"),
                     ("git", "remote", "set-head"), ("git", "pull")):
        assert any(p[:len(required)] == required for p in prefixes), \
            f"no bounded call for {required}; found {prefixes}"


def test_every_git_call_in_vault_branch_prep_is_bounded():
    """Structural regression for the vault half — all six calls, not just `pull`.

    Behavioural tests above only reach `pull` and `rev-parse`; this covers
    `checkout main` / `branch -D` / `checkout -b` and any seventh call added later.
    """
    tree = ast.parse(Path(worker.__file__).read_text(encoding="utf-8"))
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "ensure_vault_branch_fresh"), None)
    assert func is not None, "ensure_vault_branch_fresh not found — was it renamed?"

    calls = _bounded_git_calls(func)
    # Vacuity guard: if the wrapper were renamed, `calls` would silently be empty
    # and every assertion below would pass for free.
    assert len(calls) >= 6, f"expected the function's 6 git calls to be found, got {len(calls)}"

    unbounded = [ast.unparse(c)[:80] for c in calls
                 if not any(kw.arg == "timeout" for kw in c.keywords)]
    assert unbounded == [], f"git calls without a timeout: {unbounded}"

    prefixes = [_first_arg_words(c) for c in calls]
    for required in (("git", "branch", "--show-current"), ("git", "checkout", "main"),
                     ("git", "pull"), ("git", "rev-parse"),
                     ("git", "branch", "-D"), ("git", "checkout", "-b")):
        assert any(p[:len(required)] == required for p in prefixes), \
            f"no bounded call for {required}; found {prefixes}"
