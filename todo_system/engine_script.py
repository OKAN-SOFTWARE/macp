"""Run an engine shell script from whichever layout the engine is in.

::

    python -m todo_system.engine_script [--dry-run] <name> [args...]
    python -m todo_system.engine_script --list

The engine's shell entry points (``scripts/orchestrator_tick.sh``,
``scripts/kakao_send.sh``, ...) live beside the package in a development
checkout and under ``todo_system/_engine/scripts/`` in an installed wheel
(``pyproject.toml`` force-includes them). Daemon units and operators used to
spell the checkout path; this module is the one entry point that works in both
layouts (public split step 7, P3): it resolves ``paths.engine_root() / "scripts"
/ <name>`` and ``os.execv``s it, so the script replaces this process and keeps
its argv, env, cwd and exit status untouched. Nothing here reads
``MACP_VAULT_ROOT`` — engine files ship with the code; each script resolves the
vault for itself, exactly as it does when run by path.

Rules:

* ``<name>`` is a bare file name in that directory. Anything with a path
  separator, ``.``/``..`` or a leading ``-`` is refused (no escaping the
  directory, no option mistaken for a name). An unknown name exits 2 and lists
  the names that would have worked.
* ``*.py`` scripts run under ``sys.executable`` (the caller's interpreter and
  site-packages, not whatever ``python3`` the shebang finds); everything else
  is executed directly, so its shebang decides. A script that lost its execute
  bit on the way through a wheel gets it back before the exec (best effort).
* ``--dry-run`` prints the resolved path, the exact argv that would be
  executed (JSON) and whether the file is executable, then exits 0 without
  running anything. It exists because the tick scripts ignore their arguments:
  ``orchestrator_tick.sh --help`` runs a real tick. The installed-wheel smoke
  test uses the dry run to prove the resolution lands in site-packages.
* Exit codes: 0 (dry run / listing), 2 (usage, unknown name), 126 (resolved
  but could not be executed).
* Right before the exec, ``MACP_PYTHON`` is set to ``sys.executable`` unless
  the caller already set it (13b). The daemon scripts read it only in an
  installed wheel — where ``$ENGINE_DIR`` (``todo_system/_engine``) ships no
  ``pyproject.toml`` for ``uv run --project`` and is not a package for
  ``PYTHONPATH`` — and run ``"$MACP_PYTHON" -P -m todo_system...`` there. A
  checkout never reads it, and a dry run sets nothing.

Stdlib-only and 3.9-safe like ``paths`` (bare ``python3`` daemons import it).
No literal from the identifier gate's pattern may appear in this file: it sits
inside the scanned tree.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from . import paths

USAGE = (
    "usage: python -m todo_system.engine_script [--dry-run] <name> [args...]\n"
    "       python -m todo_system.engine_script --list\n"
)
EXIT_USAGE = 2
EXIT_CANNOT_EXEC = 126
# Listed even without an execute bit: a wheel may have dropped the bit, and the
# exec path restores it. Anything else needs the bit to count as a script.
SCRIPT_SUFFIXES = (".sh", ".zsh", ".py")


def scripts_dir(package_dir: Path | None = None) -> Path:
    """``engine_root()/scripts`` — checkout ``scripts/`` or the wheel's
    ``todo_system/_engine/scripts/``. ``package_dir`` is the ``paths`` test seam."""
    return paths.engine_root(package_dir) / "scripts"


def available(package_dir: Path | None = None) -> list[str]:
    """Sorted names of the runnable scripts: regular, non-hidden files directly
    in :func:`scripts_dir` that carry a script suffix or an execute bit.
    Subdirectories and documents (``*.md``, ``*.txt``) are not scripts."""
    directory = scripts_dir(package_dir)
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    names = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_file():
            continue
        if entry.suffix in SCRIPT_SUFFIXES or os.access(entry, os.X_OK):
            names.append(entry.name)
    return names


def resolve(name: str, package_dir: Path | None = None) -> Path | None:
    """The absolute path for ``name``, or None when it is not a bare name of a
    script in :func:`scripts_dir` (paths, ``.``/``..`` and options are refused
    rather than resolved)."""
    if not name or name in (".", "..") or name.startswith("-"):
        return None
    separators = {"/", os.sep}
    if os.altsep:
        separators.add(os.altsep)
    if any(sep in name for sep in separators):
        return None
    if name not in available(package_dir):
        return None
    return scripts_dir(package_dir) / name


def exec_argv(path: Path) -> list[str]:
    """argv prefix for ``path``: ``[sys.executable, path]`` for ``*.py`` (the
    caller's interpreter, so an installed wheel's scripts see its
    site-packages), ``[path]`` otherwise (the kernel honours the shebang)."""
    if path.suffix == ".py":
        return [sys.executable, str(path)]
    return [str(path)]


def _listing(package_dir: Path | None) -> str:
    names = available(package_dir)
    lines = [f"available ({scripts_dir(package_dir)}):"]
    lines.extend(f"  {n}" for n in names)
    if not names:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def _ensure_executable(path: Path) -> None:
    """Best-effort ``chmod +x`` for a non-Python script that lost its bit (a
    wheel built without mode preservation). Failures are left to the exec,
    which reports them."""
    if path.suffix == ".py" or os.access(path, os.X_OK):
        return
    try:
        mode = path.stat().st_mode
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def main(argv: list[str] | None = None, *, package_dir: Path | None = None,
         exec_fn=None) -> int:
    """Entry point. ``argv`` defaults to ``sys.argv[1:]``. Options are read only
    *before* ``<name>``; everything after it belongs to the script. ``exec_fn``
    (test seam) replaces ``os.execv``; when it returns, so does ``main`` with 0.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = False
    while args and args[0].startswith("-"):
        opt = args.pop(0)
        if opt == "--dry-run":
            dry_run = True
        elif opt in ("--list", "-h", "--help"):
            sys.stdout.write(USAGE + _listing(package_dir))
            return 0
        elif opt == "--":
            break
        else:
            sys.stderr.write(f"engine_script: unknown option: {opt}\n{USAGE}")
            return EXIT_USAGE
    if not args:
        sys.stderr.write(USAGE + _listing(package_dir))
        return EXIT_USAGE

    name, rest = args[0], args[1:]
    path = resolve(name, package_dir)
    if path is None:
        sys.stderr.write(f"engine_script: no such engine script: {name}\n"
                         + _listing(package_dir))
        return EXIT_USAGE

    argv_out = exec_argv(path) + rest
    if dry_run:
        executable = "true" if os.access(path, os.X_OK) else "false"
        sys.stdout.write(f"script={path}\nargv={json.dumps(argv_out)}\n"
                         f"executable={executable}\n")
        return 0

    _ensure_executable(path)
    # The daemon scripts' interpreter seam (13b): in an installed wheel they run
    # `"$MACP_PYTHON" -P -m todo_system...` instead of `uv run --project
    # "$ENGINE_DIR"`, which has no pyproject there. A caller's own value wins and
    # a checkout (pyproject present) ignores it; execv hands os.environ down.
    os.environ.setdefault("MACP_PYTHON", sys.executable)
    run = exec_fn if exec_fn is not None else os.execv
    # execv replaces the process: anything still buffered would be lost.
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        run(argv_out[0], argv_out)
    except OSError as exc:
        sys.stderr.write(f"engine_script: cannot execute {path}: {exc}\n")
        return EXIT_CANNOT_EXEC
    return 0


if __name__ == "__main__":
    sys.exit(main())
