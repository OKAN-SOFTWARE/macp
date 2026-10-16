"""Split install: the worker LaunchAgent hands the vault location to its daemon.

Notes/macp-public-split-inventory.md §11, leaf 11j (follow-up to 11i).

After 11i, install_worker_launchagent.sh resolved VAULT_DIR = MACP_VAULT_ROOT >
ENGINE_DIR at install time and baked it into the plist's WorkingDirectory and
log paths, but never exported it to the daemon: EnvironmentVariables carried
PATH only. launchd starts worker_run_next.sh with that environment, so in a
split install the runner saw no MACP_VAULT_ROOT, fell back to VAULT_DIR =
ENGINE_DIR, cd'd into the engine checkout and treated it as the vault. The
plist now always carries MACP_VAULT_ROOT=$VAULT_DIR. In the combined layout
that value is the runner's own ENGINE_DIR, so its split-only branch
(`[[ "$VAULT_DIR" != "$ENGINE_DIR" ]]`) stays off.

Same harness as 11i (kept self-contained so each leaf stands alone): each case
runs a *copy* of the script under tmp_path with HOME pointed at tmp and a fake
launchctl first on PATH, then checks the rendered plist statically.
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
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{_LABEL}.plist"

    @property
    def expected_path(self) -> str:
        return f"/opt/homebrew/bin:{self.home}/.local/bin:/usr/local/bin:/usr/bin:/bin"

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
    return plistlib.loads(h.plist_path.read_bytes())


def test_split_exports_vault_root_to_daemon(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")

    r = h.run(engine / _SCRIPT_REL, MACP_VAULT_ROOT=str(vault))

    plist = _assert_ran(h, r)
    env = plist["EnvironmentVariables"]
    assert env["MACP_VAULT_ROOT"] == str(vault), plist
    assert env["MACP_VAULT_ROOT"] == plist["WorkingDirectory"], plist
    assert env["MACP_VAULT_ROOT"] != str(engine), plist
    assert not Path(env["MACP_VAULT_ROOT"]).is_relative_to(engine), plist
    assert env["PATH"] == h.expected_path, plist
    assert set(env) == {"PATH", "MACP_VAULT_ROOT"}, plist


def test_combined_exports_engine_dir(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "elsewhere" / "macp")

    r = h.run(engine / _SCRIPT_REL)

    plist = _assert_ran(h, r)
    env = plist["EnvironmentVariables"]
    assert env["MACP_VAULT_ROOT"] == str(engine) == plist["WorkingDirectory"], plist
    assert plist["ProgramArguments"][1] == str(engine / _RUNNER_REL), plist
    assert env["PATH"] == h.expected_path, plist
    assert set(env) == {"PATH", "MACP_VAULT_ROOT"}, plist


def test_combined_value_matches_runner_engine_dir(tmp_path):
    """The runtime no-op: worker_run_next.sh computes ENGINE_DIR from its own
    path the same way the installer does, so the exported value compares equal
    and the runner's split-only branch is not taken."""
    h = _Harness(tmp_path.resolve())
    engine = _engine(tmp_path.resolve() / "elsewhere" / "macp")

    r = h.run(engine / _SCRIPT_REL)

    plist = _assert_ran(h, r)
    runner = plist["ProgramArguments"][1]
    got = subprocess.run(["zsh", "-c", 'cd "$(dirname "$1")/.." && pwd', "_", runner],
                         capture_output=True, text=True, timeout=30)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == plist["EnvironmentVariables"]["MACP_VAULT_ROOT"], plist


def test_installer_declares_vault_root_env():
    lines = (_REAL_ENGINE / _SCRIPT_REL).read_text(encoding="utf-8").splitlines()
    stripped = [line.strip() for line in lines]
    assert "<key>PATH</key>" in stripped
    assert stripped.count("<key>MACP_VAULT_ROOT</key>") == 1
    at = stripped.index("<key>MACP_VAULT_ROOT</key>")
    assert stripped[at + 1] == "<string>${VAULT_DIR}</string>"
