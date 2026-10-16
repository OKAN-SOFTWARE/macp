"""Split install: worker_run_next.sh imports code from the engine, keeps data in the vault.

Notes/macp-public-split-inventory.md §11, leaf 11h (same defect as 11g).

worker_run_next.sh used one anchor for both code and data: VAULT_DIR =
MACP_VAULT_ROOT or a hardcoded $HOME/Projects/MaestroACP, then `cd` there and
`uv run python -m todo_system.worker maintenance`. todo_system is not installed
(package = false), so the import came from the cwd: in a split install (engine
checkout != vault) the vault has no package and maintenance died on the import,
or a stale copy in the vault ran instead of the engine. A checkout anywhere
else than the hardcoded path sent the script to a directory it then created.
The script now has two anchors: ENGINE_DIR (script parent) for code, and
VAULT_DIR = MACP_VAULT_ROOT > ENGINE_DIR for data (logs/ and the cwd). `uv run
--project "$ENGINE_DIR"` picks the engine without moving the cwd. A split
install exports MACP_VAULT_ROOT and puts the engine on PYTHONPATH with
`python -P`; the combined layout leaves env and argv alone.

Unlike orchestrator_tick.sh, this script prepends $HOME/.local/bin (and a few
system dirs) to PATH itself, so a fake uv at the front of the caller's PATH
would lose to a real one there. The harness points HOME at tmp and puts the
fake uv in $HOME/.local/bin, and every case first checks the fake was the one
called. The call is piped through `tee`, so the exit code says nothing: every
case judges by the call log. Each case runs a *copy* of the script under
tmp_path with a stub todo_system.worker, never the real one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_SCRIPT_REL = Path("scripts") / "worker_run_next.sh"
_WORKER_REL = Path("todo_system") / "worker.py"

pytestmark = [
    pytest.mark.skipif(shutil.which("zsh") is None,
                       reason="worker_run_next.sh is a zsh script"),
    pytest.mark.skipif(Path("/opt/homebrew/bin/uv").exists(),
                       reason="script prepends /opt/homebrew/bin; a real uv there "
                              "would win over the fake"),
]

# Records the call, then runs the real interpreter (the one running pytest) so
# the import is resolved exactly as it would be: from the cwd, PYTHONPATH, -P.
_FAKE_UV = f'''#!{sys.executable}
import json, os, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["TICK_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "uv", "argv": argv, "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT"),
                         "PYTHONPATH": os.environ.get("PYTHONPATH")}}) + "\\n")
assert argv and argv[0] == "run", argv
rest = argv[1:]
while rest and rest[0].startswith("--"):
    flag, value, rest = rest[0], rest[1], rest[2:]
    if flag == "--directory":
        os.chdir(value)
assert rest and rest[0] == "python", rest
sys.exit(subprocess.run([sys.executable, *rest[1:]], env=os.environ).returncode)
'''

_STUB_WORKER = '''import json, os, sys
from pathlib import Path
with open(os.environ["TICK_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "worker", "file": str(Path(__file__).resolve()),
                         "argv": sys.argv[1:], "cwd": os.getcwd(),
                         "MACP_VAULT_ROOT": os.environ.get("MACP_VAULT_ROOT"),
                         "PYTHONPATH": os.environ.get("PYTHONPATH")}) + "\\n")
print("stub-worker", sys.argv[1:])
'''


def _package(root: Path) -> None:
    (root / "todo_system").mkdir(parents=True)
    (root / "todo_system" / "__init__.py").write_text("", encoding="utf-8")
    (root / _WORKER_REL).write_text(_STUB_WORKER, encoding="utf-8")


def _engine(root: Path) -> Path:
    """Code only: a worker_run_next.sh copy and a stub todo_system.worker."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_REAL_ENGINE / _SCRIPT_REL, root / _SCRIPT_REL)
    _package(root)
    return root


def _vault(root: Path, *, decoy: bool = False) -> Path:
    """Data only. decoy=True leaves a stale package copy that must not win."""
    root.mkdir(parents=True, exist_ok=True)
    if decoy:
        _package(root)
    return root


class _Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.log = tmp / "calls.jsonl"
        self.home = tmp / "home"
        fake_bin = self.home / ".local" / "bin"
        fake_bin.mkdir(parents=True)
        (fake_bin / "uv").write_text(_FAKE_UV, encoding="utf-8")
        (fake_bin / "uv").chmod(0o755)

    @property
    def old_fallback(self) -> Path:
        """Where the pre-11h script went when MACP_VAULT_ROOT was unset."""
        return self.home / "Projects" / "MaestroACP"

    def run(self, script: Path, **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_VAULT_ROOT / PYTHONPATH. The script
        # puts $HOME/.local/bin ahead of the caller's PATH, so HOME is tmp.
        full = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "TICK_TEST_LOG": str(self.log),
            "PYTHONUTF8": "1",
            **env,
        }
        return subprocess.run(["zsh", str(script)], env=full, capture_output=True,
                              text=True, timeout=60)

    def calls(self, tool: str) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [r for r in rows if r["tool"] == tool]


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _one_run(h: _Harness, r) -> tuple[dict, dict]:
    # The fake uv first: an empty uv log means a real uv ran instead.
    uv = h.calls("uv")
    assert len(uv) == 1, (uv, r.stdout, r.stderr)
    # Then the stub: a broken import leaves only the uv row, and the rc is tee's.
    worker = h.calls("worker")
    assert len(worker) == 1, (uv, worker, r.stdout, r.stderr)
    assert worker[0]["argv"] == ["maintenance"], worker
    return uv[0], worker[0]


def _log_text(root: Path) -> str:
    return (root / "logs" / "worker.out.log").read_text(encoding="utf-8")


def _assert_split(h: _Harness, r, engine: Path, vault: Path) -> None:
    uv, worker = _one_run(h, r)
    assert worker["file"] == str(engine / _WORKER_REL), worker
    assert worker["cwd"] == str(vault), worker
    assert worker["MACP_VAULT_ROOT"] == str(vault), worker
    assert (worker["PYTHONPATH"] or "").split(os.pathsep)[0] == str(engine), worker

    assert _opt(uv["argv"], "--project") == str(engine), uv
    assert "--directory" not in uv["argv"], uv
    assert "-P" in uv["argv"], uv
    assert uv["cwd"] == str(vault), uv

    text = _log_text(vault)
    assert "worker_run_next: maintenance" in text
    assert "stub-worker" in text
    assert not (engine / "logs").exists()
    assert not h.old_fallback.exists()


def test_split_code_from_engine_data_in_vault(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault")

    r = h.run(engine / _SCRIPT_REL, MACP_VAULT_ROOT=str(vault))

    _assert_split(h, r, engine, vault)


def test_split_vault_copy_of_package_does_not_shadow_engine(tmp_path):
    h = _Harness(tmp_path)
    engine = _engine(tmp_path / "engine")
    vault = _vault(tmp_path / "vault", decoy=True)

    r = h.run(engine / _SCRIPT_REL, MACP_VAULT_ROOT=str(vault))

    _assert_split(h, r, engine, vault)


@pytest.mark.parametrize("env_mode", ["unset", "same_dir"])
def test_combined_default_install_resolves_as_before(tmp_path, env_mode):
    """The LaunchAgent's default install: the checkout sits at the path the
    pre-11h script hardcoded, and the plist passes no MACP_VAULT_ROOT. Old and
    new fallback are the same directory, and the worker's env is left alone."""
    h = _Harness(tmp_path)
    combined = _engine(h.old_fallback)
    env = {"MACP_VAULT_ROOT": str(combined)} if env_mode == "same_dir" else {}

    r = h.run(combined / _SCRIPT_REL, **env)

    uv, worker = _one_run(h, r)
    assert worker["file"] == str(combined / _WORKER_REL), worker
    assert worker["cwd"] == str(combined), worker
    assert worker["MACP_VAULT_ROOT"] == env.get("MACP_VAULT_ROOT"), worker
    assert worker["PYTHONPATH"] is None, worker
    assert uv["cwd"] == str(combined), uv
    assert "-P" not in uv["argv"], uv
    assert "stub-worker" in _log_text(combined)


def test_combined_nonstandard_location_uses_script_parent(tmp_path):
    """A checkout anywhere else: the pre-11h script ignored it, created the
    hardcoded directory and failed the import there."""
    h = _Harness(tmp_path)
    combined = _engine(tmp_path / "elsewhere" / "macp")

    r = h.run(combined / _SCRIPT_REL)

    uv, worker = _one_run(h, r)
    assert worker["file"] == str(combined / _WORKER_REL), worker
    assert worker["cwd"] == str(combined), worker
    assert worker["MACP_VAULT_ROOT"] is None and worker["PYTHONPATH"] is None, worker
    assert "-P" not in uv["argv"], uv
    assert "stub-worker" in _log_text(combined)
    assert not h.old_fallback.exists()


def test_worker_run_next_declares_two_anchors():
    text = (_REAL_ENGINE / _SCRIPT_REL).read_text(encoding="utf-8")
    for needle in (
        'ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"',
        'VAULT_DIR="${MACP_VAULT_ROOT:-$ENGINE_DIR}"',
        'export MACP_VAULT_ROOT="$VAULT_DIR"',
        'uv run --project "$ENGINE_DIR"',
        'LOG_DIR="$VAULT_DIR/logs"',
        'cd "$VAULT_DIR"',
        'export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"',
    ):
        assert needle in text, needle
    assert "uv run --directory" not in text
    assert "Projects/MaestroACP" not in text
