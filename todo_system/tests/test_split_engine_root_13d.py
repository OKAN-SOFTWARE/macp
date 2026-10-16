"""Public split 13d: the LaunchAgent installers that start an engine script can
render an installed-mode plist that runs it from the wheel.

Notes/macp-public-split-inventory.md, leaf 13d (13c's gen_units pattern applied
to the Mac installers).

The default mode is pinned byte for byte: with MACP_ENGINE_PYTHON unset or empty
each installer writes exactly the plist text it wrote before 13d. The expected
texts below were captured from the pre-13d installers (tmp paths tokenised as
@HOME@ / @ENGINE@ / @VAULT@), not typed by hand.

Same harness as 11k (kept self-contained so the leaf stands alone): each case
runs a *copy* of the installer under tmp_path with HOME pointed at tmp and a
fake launchctl first on PATH. install_health also calls `plutil -lint`, which
only exists on macOS, so a fake plutil that parses the file with plistlib
stands in for it. The real launchctl is never called.
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

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="the LaunchAgent installers are zsh scripts")

# installer -> (label, engine script name, extra installer env)
_CASES = {
    "brief": ("install_brief_launchagent.sh", "com.macp.secondbrain.brief", "brief.sh", {}),
    "orchestrator": ("install_orchestrator_launchagent.sh",
                     "com.macp.secondbrain.orchestrator", "orchestrator_tick.sh", {}),
    "health": ("install_health_launchagent.sh", "com.macp.secondbrain.health",
               "health_report.py", {}),
    # KAKAO_REFRESH_INSTALL=1 opts in past the default-off guard (Slack-only
    # cutover): the installer refuses to install without it, so the real install
    # path these tests exercise requires the opt-in.
    "kakao_refresh": ("install_kakao_refresh_launchagent.sh",
                      "com.macp.secondbrain.kakao-refresh", "kakao_token_refresh.sh",
                      {"KAKAO_REFRESH_INSTALL": "1",
                       "KAKAO_REST_API_KEY": "test-rest-key",
                       "KAKAO_CLIENT_SECRET": "test-client-secret"}),
    "kakao_refresh_nosecret": ("install_kakao_refresh_launchagent.sh",
                               "com.macp.secondbrain.kakao-refresh", "kakao_token_refresh.sh",
                               {"KAKAO_REFRESH_INSTALL": "1",
                                "KAKAO_REST_API_KEY": "test-rest-key"}),
}

# `print` fails so the bootout branch is skipped (nothing loaded yet); every
# other call is recorded and succeeds.
_FAKE_LAUNCHCTL = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["LAUNCHCTL_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
sys.exit(1 if argv and argv[0] == "print" else 0)
'''
# Records its argv and really lints: a malformed plist raises and exits non-zero.
_FAKE_PLUTIL = f'''#!{sys.executable}
import json, os, plistlib, sys
argv = sys.argv[1:]
with open(os.environ["PLUTIL_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
with open(argv[-1], "rb") as fh:
    plistlib.loads(fh.read())
print(argv[-1] + ": OK")
'''

_BRIEF = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.macp.secondbrain.brief</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>@ENGINE@/scripts/brief.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>@VAULT@</string>

  <!-- 08:30, not 06:30: the Claude weekly quota resets at 08:00 KST. On the last
       morning of a heavy week a 06:30 run dies at the "claude -p" step
       ("hit your weekly limit · resets 8am") and the briefing silently goes
       missing — the earlier steps still run, so only the document is absent.
       Keep this after 08:00. (2026-08-04/05 both lost their brief this way.) -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:@HOME@/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>MACP_VAULT_ROOT</key>
    <string>@VAULT@</string>
  </dict>

  <key>StandardOutPath</key>
  <string>@VAULT@/logs/brief.out.log</string>

  <key>StandardErrorPath</key>
  <string>@VAULT@/logs/brief.err.log</string>
</dict>
</plist>
"""

_ORCHESTRATOR = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.macp.secondbrain.orchestrator</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>@ENGINE@/scripts/orchestrator_tick.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>@VAULT@</string>

  <key>StartInterval</key>
  <integer>300</integer>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:@HOME@/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>MACP_VAULT_ROOT</key>
    <string>@VAULT@</string>
  </dict>

  <key>StandardOutPath</key>
  <string>@VAULT@/logs/orchestrator.out.log</string>

  <key>StandardErrorPath</key>
  <string>@VAULT@/logs/orchestrator.err.log</string>
</dict>
</plist>
"""

_HEALTH = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.macp.secondbrain.health</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>@VAULT@/scripts/health_report.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>@VAULT@</string>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>0</integer>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:@HOME@/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>

  <key>StandardOutPath</key>
  <string>@VAULT@/logs/health.out.log</string>

  <key>StandardErrorPath</key>
  <string>@VAULT@/logs/health.err.log</string>
</dict>
</plist>
"""

_KAKAO_REFRESH = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.macp.secondbrain.kakao-refresh</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>@VAULT@/scripts/kakao_token_refresh.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>@VAULT@</string>

  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>30</integer></dict>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:@HOME@/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>KAKAO_REST_API_KEY</key>
    <string>test-rest-key</string>
    <key>KAKAO_CLIENT_SECRET</key>
    <string>test-client-secret</string>
  </dict>

  <key>StandardOutPath</key>
  <string>@VAULT@/logs/kakao-refresh.out.log</string>

  <key>StandardErrorPath</key>
  <string>@VAULT@/logs/kakao-refresh.err.log</string>
</dict>
</plist>
"""

# Without a secret the installer still emits the (empty) entry line.
_KAKAO_REFRESH_NOSECRET = _KAKAO_REFRESH.replace(
    "    <key>KAKAO_CLIENT_SECRET</key>\n    <string>test-client-secret</string>", "")

_EXPECTED = {
    "brief": _BRIEF,
    "orchestrator": _ORCHESTRATOR,
    "health": _HEALTH,
    "kakao_refresh": _KAKAO_REFRESH,
    "kakao_refresh_nosecret": _KAKAO_REFRESH_NOSECRET,
}


class _Harness:
    def __init__(self, tmp: Path, case: str):
        self.case = case
        self.installer, self.label, self.name, self.case_env = _CASES[case]
        self.tmp = tmp.resolve()
        self.log = self.tmp / "launchctl.jsonl"
        self.plutil_log = self.tmp / "plutil.jsonl"
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.engine = self.tmp / "engine"
        self.vault = self.tmp / "vault"
        for d in (self.home, self.bin, self.engine / "scripts", self.vault):
            d.mkdir(parents=True)
        for tool, body in (("launchctl", _FAKE_LAUNCHCTL), ("plutil", _FAKE_PLUTIL)):
            (self.bin / tool).write_text(body, encoding="utf-8")
            (self.bin / tool).chmod(0o755)
        self.script = self.engine / "scripts" / self.installer
        shutil.copy2(_REAL_ENGINE / "scripts" / self.installer, self.script)

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self.label}.plist"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_ENGINE_PYTHON / MACP_LABEL_PREFIX.
        full = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "LAUNCHCTL_TEST_LOG": str(self.log),
            "PLUTIL_TEST_LOG": str(self.plutil_log),
            "MACP_VAULT_ROOT": str(self.vault),
            **self.case_env,
            **env,
        }
        return subprocess.run(["zsh", str(self.script), *args], env=full,
                              capture_output=True, text=True, timeout=60)

    def expected_text(self) -> str:
        return (_EXPECTED[self.case].replace("@HOME@", str(self.home))
                .replace("@ENGINE@", str(self.engine)).replace("@VAULT@", str(self.vault)))

    def plist(self) -> dict:
        return plistlib.loads(self.plist_path.read_bytes())

    def calls(self) -> list[list[str]]:
        return _jsonl(self.log)

    def plutil_calls(self) -> list[list[str]]:
        return _jsonl(self.plutil_log)


def _jsonl(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _assert_installed(h: _Harness, r: subprocess.CompletedProcess) -> None:
    assert r.returncode == 0, (r.stdout, r.stderr)
    # An empty log means a real launchctl ran instead of the fake.
    uid = os.getuid()
    assert h.calls() == [
        ["print", f"gui/{uid}/{h.label}"],
        ["bootstrap", f"gui/{uid}", str(h.plist_path)],
        ["enable", f"gui/{uid}/{h.label}"],
    ], (h.calls(), r.stdout, r.stderr)
    if h.case == "health":
        assert h.plutil_calls() == [["-lint", str(h.plist_path)]], h.plutil_calls()


@pytest.mark.parametrize("knob", ["unset", "empty"])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_default_mode_plist_is_byte_identical(tmp_path, case, knob):
    h = _Harness(tmp_path, case)
    env = {"MACP_ENGINE_PYTHON": ""} if knob == "empty" else {}

    r = h.run(**env)

    _assert_installed(h, r)
    assert h.plist_path.read_bytes() == h.expected_text().encode("utf-8")
    h.plist()  # and it still parses


@pytest.mark.parametrize("case", sorted(_CASES))
def test_installed_mode_program_arguments(tmp_path, case):
    h = _Harness(tmp_path, case)
    _assert_installed(h, h.run())
    default = h.plist()
    h.log.unlink()
    h.plutil_log.unlink(missing_ok=True)
    py = str(h.tmp / "venv" / "bin" / "python")

    r = h.run(MACP_ENGINE_PYTHON=py)

    _assert_installed(h, r)
    plist = h.plist()
    assert plist["ProgramArguments"] == [py, "-P", "-m", "todo_system.engine_script", h.name]
    env = plist["EnvironmentVariables"]
    assert env["MACP_VAULT_ROOT"] == str(h.vault) == plist["WorkingDirectory"], plist
    assert "MACP_PYTHON" not in env, env  # engine_script sets it before the exec
    # Only ProgramArguments changes, plus MACP_VAULT_ROOT where it was missing.
    assert env == {**default["EnvironmentVariables"], "MACP_VAULT_ROOT": str(h.vault)}, env
    keys = {"PATH", "MACP_VAULT_ROOT"}
    if case.startswith("kakao_refresh"):
        # KAKAO_REFRESH_INSTALL is the opt-in gate, not a plist EnvironmentVariables
        # key — only the Kakao credentials are embedded in the plist.
        keys |= set(h.case_env) - {"KAKAO_REFRESH_INSTALL"}
    assert set(env) == keys, env
    assert {**plist, "ProgramArguments": None, "EnvironmentVariables": None} == \
        {**default, "ProgramArguments": None, "EnvironmentVariables": None}


@pytest.mark.parametrize("case", ["brief", "orchestrator", "health", "kakao_refresh"])
def test_relative_engine_python_is_refused(tmp_path, case):
    h = _Harness(tmp_path, case)

    r = h.run(MACP_ENGINE_PYTHON="venv/bin/python")

    assert r.returncode != 0, (r.stdout, r.stderr)
    assert "absolute" in r.stderr, r.stderr
    assert not h.plist_path.exists()
    assert not (h.home / "Library").exists()
    assert h.calls() == [] and h.plutil_calls() == []


def test_kakao_refresh_installer_is_default_off_without_opt_in(tmp_path):
    """Slack-only cutover: the Kakao refresh LaunchAgent installer refuses to
    install unless KAKAO_REFRESH_INSTALL=1. A plain run is a no-op SUCCESS (exit 0,
    nothing written, no launchctl), so it never errors a caller — it just declines."""
    h = _Harness(tmp_path, "kakao_refresh")

    r = h.run(KAKAO_REFRESH_INSTALL="")  # override the opt-in that _CASES sets

    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "기본 비활성" in r.stderr, r.stderr
    assert not h.plist_path.exists()
    assert not (h.home / "Library").exists()
    assert h.calls() == [] and h.plutil_calls() == []


def test_health_dry_run_installed_mode_skips_launchctl(tmp_path):
    h = _Harness(tmp_path, "health")
    py = str(h.tmp / "venv" / "bin" / "python")

    r = h.run("--dry-run", MACP_ENGINE_PYTHON=py)

    assert r.returncode == 0, (r.stdout, r.stderr)
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("DRY_RUN: plist written to "))
    path = Path(line[len("DRY_RUN: plist written to "):].split(" (", 1)[0])
    try:
        plist = plistlib.loads(path.read_bytes())
        assert plist["ProgramArguments"] == [py, "-P", "-m", "todo_system.engine_script",
                                             "health_report.py"]
        assert plist["EnvironmentVariables"]["MACP_VAULT_ROOT"] == str(h.vault)
        assert h.plutil_calls() == [["-lint", str(path)]]
    finally:
        shutil.rmtree(path.parent)
    assert h.calls() == []
    assert not h.plist_path.exists()


def test_out_of_scope_installers_have_no_knob():
    """Vault-only and `uv run python` installers are untouched (the ones the 13d
    spec left out by name were converted in 13e)."""
    for name in ("remote_control", "runner", "todo_sync"):
        path = _REAL_ENGINE / "scripts" / f"install_{name}_launchagent.sh"
        if not path.is_file():
            continue  # vault-only (scripts/public_export_exclude.txt): absent from an export
        text = path.read_text(encoding="utf-8")
        assert "MACP_ENGINE_PYTHON" not in text, name
        assert "engine_script" not in text, name
