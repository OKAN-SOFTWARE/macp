"""Split install: the worker LaunchAgent runs engine code, keeps its data in the vault.

Notes/macp-public-split-inventory.md §11, leaf 11i (same defect as 11h).

install_worker_launchagent.sh used one anchor for both code and data: VAULT_DIR
= MACP_VAULT_ROOT or a hardcoded $HOME/Projects/MaestroACP resolved the plist's
ProgramArguments[1] (scripts/worker_run_next.sh, code) as well as its
WorkingDirectory and log paths (data). In a split install (engine checkout !=
vault) the plist pointed at a runner the vault does not have, and a checkout
anywhere else than the hardcoded path got a plist aimed at that path, whose
logs/ the installer then created. The script now has two anchors: ENGINE_DIR
(script parent) for the runner path, and VAULT_DIR = MACP_VAULT_ROOT >
ENGINE_DIR for logs/ and the WorkingDirectory. The launchctl calls are
unchanged; 11j later added MACP_VAULT_ROOT to the plist's EnvironmentVariables
(test_split_engine_root_11j.py).

Each case runs a *copy* of the script under tmp_path with HOME pointed at tmp,
so the plist lands in a tmp LaunchAgents dir. The script never touches PATH, so
a fake launchctl at the front of the caller's PATH wins; every case first
checks the fake recorded the calls (this host has no real launchctl either).
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
_SCRIPT_REL = Path("scripts") / "install_worker_launchagent.sh"
_RUNNER_REL = Path("scripts") / "worker_run_next.sh"
_LABEL = "com.macp.secondbrain.worker"

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="install_worker_launchagent.sh is a zsh script")

# `print` fails so the bootout branch is skipped (nothing loaded yet); every
# other call is recorded and succeeds.
_FAKE_LAUNCHCTL = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["LAUNCHCTL_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
sys.exit(1 if argv and argv[0] == "print" else 0)
'''


def _engine(root: Path) -> Path:
    """Code only: the installer copy and the runner it points the plist at."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_REAL_ENGINE / _SCRIPT_REL, root / _SCRIPT_REL)
    shutil.copy2(_REAL_ENGINE / _RUNNER_REL, root / _RUNNER_REL)
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

    @property
    def old_fallback(self) -> Path:
        """Where the pre-11i script anchored when MACP_VAULT_ROOT was unset."""
        return self.home / "Projects" / "MaestroACP"

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{_LABEL}.plist"

    def run(self, script: Path, **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_VAULT_ROOT / MACP_LABEL_PREFIX.
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
    ], (h.calls(), r.stdout, r.stderr)
    return h.plist()


def _assert_paths(plist: dict, *, runner: Path, data: Path) -> None:
    assert plist["ProgramArguments"] == ["/bin/zsh", str(runner)], plist
    assert runner.is_file(), runner
    assert plist["WorkingDirectory"] == str(data), plist
    assert plist["StandardOutPath"] == str(data / "logs" / "worker.out.log"), plist
    assert plist["StandardErrorPath"] == str(data / "logs" / "worker.err.log"), plist
    assert (data / "logs").is_dir()


def test_split_code_from_engine_data_in_vault(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")

    r = h.run(engine / _SCRIPT_REL, MACP_VAULT_ROOT=str(vault))

    plist = _assert_ran(h, r)
    _assert_paths(plist, runner=engine / _RUNNER_REL, data=vault)
    assert not (engine / "logs").exists()
    assert not h.old_fallback.exists()


@pytest.mark.parametrize("env_mode", ["unset", "same_dir"])
def test_combined_default_install_resolves_as_before(tmp_path, env_mode):
    """The default install: the checkout sits at the path the pre-11i script
    hardcoded. Old and new anchors are the same directory, so every plist value
    is the literal the old script wrote."""
    h = _Harness(tmp_path)
    combined = _engine(h.old_fallback)
    env = {"MACP_VAULT_ROOT": str(combined)} if env_mode == "same_dir" else {}

    r = h.run(combined / _SCRIPT_REL, **env)

    plist = _assert_ran(h, r)
    old = h.old_fallback
    assert plist == {
        "Label": _LABEL,
        "ProgramArguments": ["/bin/zsh", str(old / "scripts" / "worker_run_next.sh")],
        "WorkingDirectory": str(old),
        "StartInterval": 300,
        "RunAtLoad": False,
        "EnvironmentVariables": {
            "PATH": f"/opt/homebrew/bin:{h.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "MACP_VAULT_ROOT": str(old),
        },
        "StandardOutPath": str(old / "logs" / "worker.out.log"),
        "StandardErrorPath": str(old / "logs" / "worker.err.log"),
    }
    assert (old / "logs").is_dir()


def test_combined_nonstandard_location_uses_script_parent(tmp_path):
    """A checkout anywhere else: the pre-11i script ignored it, created the
    hardcoded logs/ and aimed the plist at a runner that does not exist there."""
    h = _Harness(tmp_path)
    combined = _engine(tmp_path / "elsewhere" / "macp")

    r = h.run(combined / _SCRIPT_REL)

    plist = _assert_ran(h, r)
    _assert_paths(plist, runner=combined / _RUNNER_REL, data=combined)
    assert not h.old_fallback.exists()


def test_install_launchagent_declares_two_anchors():
    text = (_REAL_ENGINE / _SCRIPT_REL).read_text(encoding="utf-8")
    for needle in (
        'ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"',
        'VAULT_DIR="${MACP_VAULT_ROOT:-$ENGINE_DIR}"',
        'SCRIPT_PATH="$ENGINE_DIR/scripts/worker_run_next.sh"',
        'LOG_DIR="$VAULT_DIR/logs"',
        '<string>${VAULT_DIR}</string>',
        'LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.worker"',
        'launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"',
    ):
        assert needle in text, needle
    assert "Projects/MaestroACP" not in text
    assert "$VAULT_DIR/scripts" not in text
