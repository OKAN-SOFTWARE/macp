"""Split install: the todo-sync LaunchAgent runs the engine's todo_system, keeps
its data in the vault, and hands the vault location to the daemon.

Notes/macp-public-split-inventory.md §11, leaf 11m (11i-11l applied to the
todo-sync installer).

install_todo_sync_launchagent.sh used one anchor for both code and data:
VAULT_DIR = MACP_VAULT_ROOT or a hardcoded path under $HOME was the plist's
WorkingDirectory, log directory and .macp/config.toml lookup, and the plist ran
a bare `uv run python -m todo_system.cli` there with no EnvironmentVariables.
In a split install (engine checkout != vault) the daemon imported whatever
todo_system sat in the vault cwd (a stale copy, or nothing) and started without
MACP_VAULT_ROOT. The script now has two anchors, ENGINE_DIR (script parent) for
the code and VAULT_DIR = MACP_VAULT_ROOT > ENGINE_DIR for logs/, config.toml and
the WorkingDirectory. The plist runs `uv run --project $ENGINE_DIR` and always
carries MACP_VAULT_ROOT=$VAULT_DIR.

`--project` alone is not enough: pyproject.toml sets package = false, so uv
never installs todo_system and `python -m` still resolves it from the cwd (the
vault). A split install therefore also puts the engine on PYTHONPATH and runs
`python -P`, the same recipe as orchestrator_tick.sh; the combined layout emits
neither (test D shows both the need and the fix at run time).

Same harness as 11i-11l (kept self-contained so each leaf stands alone): each
case runs a *copy* of the script under tmp_path with HOME pointed at tmp and a
fake launchctl first on PATH, then checks the rendered plist statically.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_SCRIPT_REL = Path("scripts") / "install_todo_sync_launchagent.sh"
_LABEL = "com.macp.secondbrain.todo-sync"
_DAEMON_TAIL = ["-u", "-m", "todo_system.cli", "daemon", "--interval", "3", "--calendar-sync"]

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="install_todo_sync_launchagent.sh is a zsh script")

# `print` fails so the bootout branch is skipped (nothing loaded yet); every
# other call is recorded and succeeds.
_FAKE_LAUNCHCTL = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["LAUNCHCTL_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
sys.exit(1 if argv and argv[0] == "print" else 0)
'''


def _stub_package(root: Path, tag: str) -> None:
    pkg = root / "todo_system"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cli.py").write_text(f'print("{tag} " + __file__)\n', encoding="utf-8")


def _engine(root: Path) -> Path:
    """Code only: the installer copy and a stub todo_system that says where it ran."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_REAL_ENGINE / _SCRIPT_REL, root / _SCRIPT_REL)
    _stub_package(root, "ENGINE")
    return root


def _vault(root: Path) -> Path:
    """Data only: an empty directory."""
    root.mkdir(parents=True)
    return root


class _Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.log = tmp / "launchctl.jsonl"
        self.home = tmp / "home"
        self.home.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "launchctl").write_text(_FAKE_LAUNCHCTL, encoding="utf-8")
        (self.bin / "launchctl").chmod(0o755)
        # The calendar-id lookup shells out to python3 (tomllib): pin it.
        (self.bin / "python3").symlink_to(sys.executable)

    @property
    def old_fallback(self) -> Path:
        """Where the pre-11m script anchored when MACP_VAULT_ROOT was unset."""
        return self.home / "Projects" / "MaestroACP"

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{_LABEL}.plist"

    @property
    def uv(self) -> str:
        return str(self.home / ".local" / "bin" / "uv")

    def run(self, script: Path, **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_VAULT_ROOT / MACP_LABEL_PREFIX /
        # MACP_UV_BIN / MACP_CALENDAR_ID.
        full = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "LAUNCHCTL_TEST_LOG": str(self.log),
            **env,
        }
        return subprocess.run(["zsh", str(script)], env=full, capture_output=True,
                              text=True, timeout=60)

    def plist(self) -> dict:
        return plistlib.loads(self.plist_path.read_bytes())

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]


def _assert_ran(h: _Harness, r) -> dict:
    assert r.returncode == 0, (r.stdout, r.stderr)
    # An empty log means a real launchctl ran instead of the fake.
    uid = os.getuid()
    assert h.calls() == [
        ["print", f"gui/{uid}/{_LABEL}"],
        ["bootstrap", f"gui/{uid}", str(h.plist_path)],
        ["enable", f"gui/{uid}/{_LABEL}"],
        ["kickstart", "-k", f"gui/{uid}/{_LABEL}"],
    ], (h.calls(), r.stdout, r.stderr)
    return h.plist()


def _run_python_leg(plist: dict, argv_tail: list[str], env: dict[str, str]):
    """Run the argv uv would exec (everything after "python") with the plist's
    cwd, and a from-scratch env so no inherited PYTHONPATH leaks in."""
    return subprocess.run([sys.executable, *argv_tail], cwd=plist["WorkingDirectory"],
                          env={"PATH": os.environ["PATH"], **env},
                          capture_output=True, text=True, timeout=60)


def test_split_code_from_engine_data_in_vault(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")

    r = h.run(engine / _SCRIPT_REL, MACP_VAULT_ROOT=str(vault))

    plist = _assert_ran(h, r)
    assert plist["ProgramArguments"] == [
        h.uv, "run", "--project", str(engine), "python", "-P", *_DAEMON_TAIL,
    ], plist
    assert plist["WorkingDirectory"] == str(vault), plist
    assert plist["StandardOutPath"] == str(vault / "logs" / "todo-sync.out.log"), plist
    assert plist["StandardErrorPath"] == str(vault / "logs" / "todo-sync.err.log"), plist
    assert plist["EnvironmentVariables"] == {
        "MACP_VAULT_ROOT": str(vault),
        "PYTHONPATH": str(engine),
    }, plist
    assert plist["StartInterval"] == 300, plist
    assert plist["RunAtLoad"] is True, plist
    assert (vault / "logs").is_dir()
    assert not (engine / "logs").exists()
    assert not h.old_fallback.exists()


@pytest.mark.parametrize("env_mode", ["unset", "same_dir"])
def test_combined_default_install_resolves_as_before(tmp_path, env_mode):
    """The default install: the checkout sits at the path the pre-11m script
    hardcoded. Old and new anchors are the same directory, so the only change
    from the old plist is `--project <checkout>` and the EnvironmentVariables
    dict with MACP_VAULT_ROOT; no -P and no PYTHONPATH."""
    h = _Harness(tmp_path)
    combined = _engine(h.old_fallback)
    env = {"MACP_VAULT_ROOT": str(combined)} if env_mode == "same_dir" else {}

    r = h.run(combined / _SCRIPT_REL, **env)

    plist = _assert_ran(h, r)
    old = h.old_fallback
    assert plist == {
        "Label": _LABEL,
        "ProgramArguments": [h.uv, "run", "--project", str(old), "python", *_DAEMON_TAIL],
        "WorkingDirectory": str(old),
        "EnvironmentVariables": {"MACP_VAULT_ROOT": str(old)},
        "RunAtLoad": True,
        "StartInterval": 300,
        "StandardOutPath": str(old / "logs" / "todo-sync.out.log"),
        "StandardErrorPath": str(old / "logs" / "todo-sync.err.log"),
    }
    assert (old / "logs").is_dir()


def test_combined_nonstandard_location_uses_script_parent(tmp_path):
    """A checkout anywhere else: the pre-11m script ignored it and ran the
    daemon in the hardcoded directory, creating its logs/ there."""
    h = _Harness(tmp_path)
    combined = _engine(tmp_path / "elsewhere" / "macp")

    r = h.run(combined / _SCRIPT_REL)

    plist = _assert_ran(h, r)
    assert plist["ProgramArguments"] == [
        h.uv, "run", "--project", str(combined), "python", *_DAEMON_TAIL,
    ], plist
    assert plist["WorkingDirectory"] == str(combined), plist
    assert plist["EnvironmentVariables"] == {"MACP_VAULT_ROOT": str(combined)}, plist
    assert (combined / "logs").is_dir()
    assert not h.old_fallback.exists()


def test_split_runs_engine_todo_system_not_vault_copy(tmp_path):
    """Run time: with a stale todo_system in the vault cwd, the split plist's
    python argv + env imports the engine's copy. RED control: the same argv
    without -P and PYTHONPATH (what --project alone gives, since package =
    false keeps todo_system uninstalled) imports the vault's stale copy."""
    tmp = tmp_path.resolve()
    h = _Harness(tmp)
    engine = _engine(tmp / "engine")
    vault = _vault(tmp / "vault")
    _stub_package(vault, "STALE")

    r = h.run(engine / _SCRIPT_REL, MACP_VAULT_ROOT=str(vault))

    plist = _assert_ran(h, r)
    argv = plist["ProgramArguments"]
    tail = argv[argv.index("python") + 1:]
    got = _run_python_leg(plist, tail, plist["EnvironmentVariables"])
    assert got.returncode == 0, got.stderr
    assert got.stdout.startswith("ENGINE "), got.stdout
    assert str(engine / "todo_system" / "cli.py") in got.stdout, got.stdout

    control = _run_python_leg(plist, [a for a in tail if a != "-P"],
                              {"MACP_VAULT_ROOT": str(vault)})
    assert control.returncode == 0, control.stderr
    assert control.stdout.startswith("STALE "), control.stdout


def test_calendar_id_still_read_from_vault_config(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")
    for root, cal in ((vault, "vault-cal"), (engine, "engine-cal")):
        (root / ".macp").mkdir()
        (root / ".macp" / "config.toml").write_text(f'calendar_id = "{cal}"\n',
                                                   encoding="utf-8")

    r = h.run(engine / _SCRIPT_REL, MACP_VAULT_ROOT=str(vault))

    plist = _assert_ran(h, r)
    argv = plist["ProgramArguments"]
    assert argv[-2:] == ["--calendar-id", "vault-cal"], plist
    assert argv[:6] == [h.uv, "run", "--project", str(engine), "python", "-P"], plist
    assert plist["EnvironmentVariables"]["PYTHONPATH"] == str(engine), plist


def test_installer_declares_two_anchors_project_and_vault_root_env():
    text = (_REAL_ENGINE / _SCRIPT_REL).read_text(encoding="utf-8")
    for needle in (
        'ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"',
        'VAULT_DIR="${MACP_VAULT_ROOT:-$ENGINE_DIR}"',
        'LOG_DIR="$VAULT_DIR/logs"',
        '"$VAULT_DIR/.macp/config.toml"',
        'LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.todo-sync"',
        'launchctl kickstart -k "gui/$(id -u)/${LABEL}"',
    ):
        assert needle in text, needle
    assert "/".join(("Projects", "MaestroACP")) not in text
    assert "$HOME/Projects" not in text

    stripped = [line.strip() for line in text.splitlines()]
    at = stripped.index("<string>--project</string>")
    assert stripped[at - 1] == "<string>run</string>"
    assert stripped[at + 1] == "<string>${ENGINE_DIR}</string>"
    assert stripped.count("<key>MACP_VAULT_ROOT</key>") == 1
    at = stripped.index("<key>MACP_VAULT_ROOT</key>")
    assert stripped[at + 1] == "<string>${VAULT_DIR}</string>"
    assert "<key>PATH</key>" not in stripped
