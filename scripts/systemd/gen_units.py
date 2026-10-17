#!/usr/bin/env python3
"""Generate the systemd --user units that replace the Mac LaunchAgents on the
cloud HQ (Ubuntu). One unit per launchd label in ``todo_system.daemonctl.UNITS``;
schedules and commands mirror ``~/Library/LaunchAgents/*.plist`` as inventoried
on 2026-09-10 (``Todo/items/macp-cloud-hq-migration.md``). The exception is
``macp-browser`` (boot-time start of the logged-in Chrome; env ``MACP_BROWSER_DIR``),
which has no launchd label and is deliberately not a daemonctl unit.

    python3 scripts/systemd/gen_units.py            # write ~/.config/systemd/user/*
    python3 scripts/systemd/gen_units.py --print    # dry run to stdout
    python3 scripts/systemd/gen_units.py --enable   # write + daemon-reload + enable --now
    python3 scripts/systemd/gen_units.py --engine-python /path/to/venv/bin/python  # installed mode (env: MACP_ENGINE_PYTHON)

Installed mode (public split 13c): units whose script ships with the engine run
``<python> -P -m todo_system.engine_script <name>`` with ``MACP_VAULT_ROOT`` set,
so they resolve the script from the installed wheel; vault-only files keep their
vault path. The units that ran ``uv run python -m todo_system...`` in the vault
(runner, maintenance, todo-sync, dashboard — public split 14) run the same module
as ``<python> -P -m ...``, so no macp unit imports the vault's own todo_system/.
Without the knob every unit is exactly what it was before.

Env: units read ``~/.config/macp/env`` (0600) — PATH/JAVA_HOME/ANDROID_HOME plus the
Kakao app secret the kakao-refresh plist carried inline. The trading units add
``~/.config/macp/kis.env``. Both files are written by ``--write-env`` from
templates below when missing (secrets left as placeholders to fill by hand).

Logs go to ``<vault>/logs/<unit>.out.log`` like the plists did, so health_report
and the monitor keep reading the same files. Stdlib-only.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
VAULT = Path(os.environ.get("MAESTRO_VAULT_DIR", HOME / "Projects" / "MaestroACP"))
MAILROOM = HOME / "Projects" / "mailroom"
TRADING = HOME / "Trading" / "ai-stock-trading"
TRADING_REAL = HOME / "Trading" / "ai-stock-trading-real"
UV = HOME / ".local" / "bin" / "uv"
ENV_FILE = HOME / ".config" / "macp" / "env"
KIS_ENV_FILE = HOME / ".config" / "macp" / "kis.env"
# Shared secret for the live dashboard; generated once by --write-env (0600).
DASHBOARD_ENV_FILE = ENV_FILE.parent / "dashboard.env"
UNIT_DIR = HOME / ".config" / "systemd" / "user"
LOGS = VAULT / "logs"
# The macp-browser checkout (browser.sh + Chrome profile). Same env knob and
# home-relative default as todo_system/browser_watch.py; never a literal path.
BROWSER_DIR = Path(os.environ.get("MACP_BROWSER_DIR") or HOME / ".config" / "macp-browser")

# Unit stems that gen_units WRITES to disk but must NEVER enable/start — not via
# ``--enable``, not via ``--enable --only <stem>``. enable_targets() drops these
# before any other filter; the unit text itself is unchanged, so a human can
# arm one by hand (``systemctl --user enable --now <stem>.timer``).
#   * kis-trading-real: trades a live brokerage account — armed by a human exactly
#     once, never by this generator.
#   * macp-kakao-refresh: since the Slack-only cutover Kakao is a rollback-only
#     backend, so its token refresh is default-OFF. The unit is still written so
#     rollback is a one-line ``systemctl --user enable --now macp-kakao-refresh.timer``.
INSTALL_ONLY: frozenset[str] = frozenset({"kis-trading-real", "macp-kakao-refresh"})

# Host-identity knobs. Neutral defaults: a live host pins its real name in
# ~/.config/macp/env (MACP_HOST_ID / CLAUDE_RC_NAME), which write_env() never
# rewrites, and these only feed ENV_TEMPLATE — so rendered units never depend on
# them. A third party overrides these to render their own env template.
HOST_ID = os.environ.get("MACP_HOST_ID", "macp-host")
CLAUDE_RC_NAME = os.environ.get("MACP_CLAUDE_RC_NAME", HOST_ID)


def _config_value(key: str) -> str:
    """Read a string ``key`` from ``<VAULT>/.macp/config.toml`` (stdlib tomllib), else "".

    gen_units runs as a bare ``python3 scripts/systemd/gen_units.py`` where
    ``todo_system`` is not on sys.path, so this reads the vault config directly
    rather than calling ``paths.primary_calendar_id()`` / ``paths.label_prefix()`` —
    same env→config→neutral resolution, no import-path dependency. Env still wins:
    callers do ``os.environ.get(<VAR>) or _config_value(<key>)``.
    """
    try:
        import tomllib
    except ImportError:
        return ""
    cfg = VAULT / ".macp" / "config.toml"
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    value = data.get(key)
    return value if isinstance(value, str) and value else ""


# Effective calendar id / label prefix: env wins, then this vault's config, then a
# neutral empty default (no personal identifier baked into the engine). An empty
# CALENDAR_ID makes the todo-sync unit omit --calendar-id so the CLI default applies.
CALENDAR_ID = os.environ.get("MACP_CALENDAR_ID") or _config_value("calendar_id")
LABEL_PREFIX = os.environ.get("MACP_LABEL_PREFIX") or _config_value("label_prefix")

ENV_TEMPLATE = f"""# MaestroACP daemon environment (systemd --user EnvironmentFile). 0600.
PATH={HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
HOME={HOME}
JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ANDROID_HOME={HOME}/Android/sdk
ANDROID_SDK_ROOT={HOME}/Android/sdk
PYTHONUNBUFFERED=1
MACP_HOST_ID={HOST_ID}
CLAUDE_RC_NAME={CLAUDE_RC_NAME}
# Pin the daemon's effective label prefix / calendar id in the process env so
# daemonctl.label_prefix() resolves them regardless of the unit's WorkingDirectory
# (env is highest priority). Empty when this host has no config/env → the resolver
# falls through to its neutral default, which is harmless.
MACP_LABEL_PREFIX={LABEL_PREFIX}
MACP_CALENDAR_ID={CALENDAR_ID}
# kakao_token_refresh.sh (was inline in the host's kakao-refresh LaunchAgent)
KAKAO_REST_API_KEY=FILL_ME
KAKAO_CLIENT_SECRET=FILL_ME
"""

KIS_ENV_TEMPLATE = f"""# kis-trading units (was inline in the host's kis-trading LaunchAgent). 0600.
KIS_UNIVERSE=etf
BUY_AND_HOLD=1
KIS_MAX_DEPLOY_KRW=1000000
# kakao_send.sh lives in this vault (the code's default is the legacy second-brain path)
KIS_ALERT_SCRIPT={VAULT}/scripts/kakao_send.sh
# Route the Slack sibling-send (slack_send.py, via kakao_send.sh) to the dedicated
# kis channel. slack_send._channel_key() reads NOTIFY_CHANNEL_KEY first; without it
# ast alerts fall to the "default" channel. KAKAO_PROFILE is untouched (Kakao path).
NOTIFY_CHANNEL_KEY=kis
"""


def service(unit: str, desc: str, exec_start: str, *, cwd: Path = VAULT,
            kind: str = "oneshot", restart: str | None = None, restart_sec: int = 30,
            extra_env: Path | None = None, wants_network: bool = True,
            extra_lines: list[str] | None = None, timeout_start_sec: int = 1800,
            unit_lines: list[str] | None = None, install: bool | None = None) -> str:
    lines = ["[Unit]", f"Description={desc}"]
    if wants_network:
        lines += ["After=network-online.target", "Wants=network-online.target"]
    lines += unit_lines or []
    lines += ["", "[Service]", f"Type={'simple' if kind == 'simple' else 'oneshot'}",
              f"WorkingDirectory={cwd}", f"EnvironmentFile={ENV_FILE}"]
    if extra_env:
        lines.append(f"EnvironmentFile={extra_env}")
    lines += [f"ExecStart={exec_start}",
              f"StandardOutput=append:{LOGS}/{unit}.out.log",
              f"StandardError=append:{LOGS}/{unit}.err.log"]
    # Type=oneshot's TimeoutStartSec default is `infinity`; a hung internal git
    # fetch/clone would then occupy the unit forever and block the next timer
    # tick. Meaningless for Type=simple (long-running daemons), so skip it there.
    if kind != "simple":
        lines.append(f"TimeoutStartSec={timeout_start_sec}")
    if restart:
        lines += [f"Restart={restart}", f"RestartSec={restart_sec}"]
    lines += extra_lines or []
    # A timerless oneshot is still an enable_targets() target; `install=True` gives
    # it the [Install] section `systemctl enable` needs (default: simple only).
    if install if install is not None else kind == "simple":
        lines += ["", "[Install]", "WantedBy=default.target"]
    return "\n".join(lines) + "\n"


def timer(unit: str, desc: str, *, every: str | None = None, calendar: list[str] | None = None,
          boot_delay: str = "2min") -> str:
    lines = ["[Unit]", f"Description={desc} (timer)", "", "[Timer]"]
    if every:
        lines += [f"OnBootSec={boot_delay}", f"OnUnitActiveSec={every}"]
    for c in calendar or []:
        lines.append(f"OnCalendar={c}")
    if calendar:
        lines.append("Persistent=true")
    lines += [f"Unit={unit}.service", "", "[Install]", "WantedBy=timers.target"]
    return "\n".join(lines) + "\n"


def build(engine_python: str | None = None) -> dict[str, str]:
    """All units. ``engine_python`` (default: env ``MACP_ENGINE_PYTHON``; empty =
    off) is the absolute interpreter of an installed engine and turns on installed
    mode for the engine-script units."""
    if engine_python is None:
        engine_python = os.environ.get("MACP_ENGINE_PYTHON", "")
    if engine_python and not Path(engine_python).is_absolute():
        raise ValueError(f"engine python must be an absolute path (systemd ExecStart): {engine_python}")

    def script(unit: str, desc: str, legacy_exec: str, name: str, **kw) -> str:
        # Default mode: exactly the legacy service() call (byte-identical units).
        if not engine_python:
            return service(unit, desc, legacy_exec, **kw)
        # -P: WorkingDirectory is the vault, which may still be a checkout with its
        # own todo_system/ — without it that copy would shadow the installed one.
        kw["extra_lines"] = [*(kw.get("extra_lines") or []), f"Environment=MACP_VAULT_ROOT={VAULT}"]
        return service(unit, desc, f"{engine_python} -P -m todo_system.engine_script {name}", **kw)

    def module(unit: str, desc: str, legacy_exec: str, installed_exec: str, **kw) -> str:
        # Resident/per-tick units that run ``python -m todo_system...`` (or a
        # Python engine script) through ``uv run`` in the vault (public split 14).
        # Installed mode runs the same command from the installed interpreter, so
        # once the vault's todo_system/ is gone nothing still imports it.
        if not engine_python:
            return service(unit, desc, legacy_exec, **kw)
        kw["extra_lines"] = [f"Environment=MACP_VAULT_ROOT={VAULT}", *(kw.get("extra_lines") or [])]
        return service(unit, desc, f"{engine_python} -P {installed_exec}", **kw)

    u: dict[str, str] = {}
    py = f"{UV} run python"

    # -- MaestroACP control plane --
    runner_args = "--agent worker-default --mode live-staged --uri file://Worker/agents --daemon"
    u["macp-runner-worker-default.service"] = module(
        "macp-runner-worker-default", "macp worker-default runner (v2 staged pipeline)",
        f"{py} -m todo_system.orchestrator.runner {runner_args}",
        f"-m todo_system.orchestrator.runner {runner_args}",
        # OOMPolicy=continue: a build the runner spawned dying to the kernel OOM
        # killer must not take the whole runner unit (and its in-flight run) down
        # with it — the default `stop` kills every process in the cgroup. Only the
        # runner gets this; the control-plane daemons should still restart on OOM.
        kind="simple", restart="always", extra_lines=["OOMPolicy=continue"])
    u["macp-orchestrator.service"] = script(
        "macp-orchestrator", "macp orchestrator tick (dispatch/collect/dashboard)",
        f"/bin/zsh {VAULT}/scripts/orchestrator_tick.sh", "orchestrator_tick.sh")
    u["macp-orchestrator.timer"] = timer("macp-orchestrator", "macp orchestrator", every="5min")
    u["macp-planner.service"] = script(
        "macp-planner", "macp planner tick", f"/bin/bash {VAULT}/scripts/planner_tick.sh",
        "planner_tick.sh")
    u["macp-planner.timer"] = timer("macp-planner", "macp planner", every="30min", boot_delay="5min")
    u["macp-maintenance.service"] = module(
        "macp-maintenance", "macp maintenance (vault sync, daemon restart, upkeep)",
        f"{py} -m todo_system.worker maintenance", "-m todo_system.worker maintenance")
    u["macp-maintenance.timer"] = timer("macp-maintenance", "macp maintenance", every="5min")
    u["macp-monitor.service"] = service(
        "macp-monitor", "macp usage monitor (sensor)",
        f"/usr/bin/python3 {VAULT}/Worker/_state/macp_monitor/monitor.py scan --daemon")
    u["macp-monitor.timer"] = timer("macp-monitor", "macp usage monitor", every="30min")
    cal_arg = f" --calendar-id {CALENDAR_ID}" if CALENDAR_ID else ""
    sync_args = f"daemon --interval 3 --calendar-sync{cal_arg}"
    u["macp-todo-sync.service"] = module(
        "macp-todo-sync", "macp todo-sync daemon (md -> todo.db, calendar)",
        f"{py} -u -m todo_system.cli {sync_args}", f"-u -m todo_system.cli {sync_args}",
        kind="simple", restart="always")
    # Loopback-only; remote access = `tailscale serve` in front of it (HTTPS on the
    # tailnet). The token file is there so the same unit can never run open if
    # someone later flips --host (the script refuses a non-loopback bind without it).
    dash_args = "--host 127.0.0.1 --port 8787 --no-open"
    u["macp-dashboard.service"] = module(
        "macp-dashboard", "macp live dashboard (loopback :8787; tailscale serve fronts it)",
        f"{py} -u scripts/macp_dashboard.py {dash_args}",
        f"-u -m todo_system.engine_script macp_dashboard.py {dash_args}",
        kind="simple", restart="always", restart_sec=10, extra_env=DASHBOARD_ENV_FILE)
    u["macp-brief.service"] = script("macp-brief", "macp morning brief", f"/bin/zsh {VAULT}/scripts/brief.sh",
                                     "brief.sh")
    u["macp-brief.timer"] = timer("macp-brief", "macp morning brief", calendar=["*-*-* 08:30:00"])
    u["macp-kakao-refresh.service"] = script(
        "macp-kakao-refresh", "Kakao token refresh", f"/bin/zsh {VAULT}/scripts/kakao_token_refresh.sh",
        "kakao_token_refresh.sh")
    u["macp-kakao-refresh.timer"] = timer(
        "macp-kakao-refresh", "Kakao token refresh", calendar=["*-*-* 00,04,08,12,16,20:30:00"])
    u["macp-health.service"] = script(
        "macp-health", "macp weekly health report", f"/usr/bin/python3 {VAULT}/scripts/health_report.py",
        "health_report.py")
    u["macp-health.timer"] = timer("macp-health", "macp weekly health report", calendar=["Sun *-*-* 08:30:00"])
    # Digest-tier kakao alerts (scripts/KAKAO_ALERTS.md) queue all day and go out
    # as ONE memo just after the morning brief. Not folded into brief.sh: the
    # brief's Claude call has died to quota exhaustion before, and the digest's
    # no-loss contract must not depend on it.
    u["macp-kakao-digest.service"] = script(
        "macp-kakao-digest", "macp kakao digest flush (queued FYI alerts → one daily memo)",
        f"/usr/bin/python3 {VAULT}/scripts/kakao_digest_send.py", "kakao_digest_send.py")
    u["macp-kakao-digest.timer"] = timer(
        "macp-kakao-digest", "macp kakao digest flush", calendar=["*-*-* 08:35:00"])

    # -- Claude CLI upkeep + Remote Control --
    # claude_auth_watch.py ships with the engine (not in public_export_exclude.txt),
    # so it goes through engine_script; MACP_VAULT_ROOT matters here (cwd is $HOME).
    u["claude-auth-watch.service"] = script(
        "claude-auth-watch", "Claude CLI credential watch",
        f"/usr/bin/python3 {VAULT}/scripts/claude_auth_watch.py", "claude_auth_watch.py", cwd=HOME)
    u["claude-auth-watch.timer"] = timer("claude-auth-watch", "Claude CLI credential watch", every="6h")
    # Vault-only from here on (the Remote Control stack, listed in
    # scripts/public_export_exclude.txt — the classifier): vault path in both modes.
    u["claude-upgrade.service"] = service(
        "claude-upgrade", "Claude CLI upgrade + remote-control restart",
        f"/usr/bin/python3 {VAULT}/scripts/claude_upgrade.py", cwd=HOME)
    u["claude-upgrade.timer"] = timer(
        "claude-upgrade", "Claude CLI upgrade", calendar=["*-*-* 04,10,16,22:40:00"])
    # First run asks "Enable Remote Control? (y/n)" on stdin and hangs forever
    # under systemd's null stdin (observed 2026-09-11; the answer is not persisted
    # in ~/.claude.json on Linux). Feed the "y" through systemd instead.
    u["claude-remote-control.service"] = service(
        "claude-remote-control", "Claude Code Remote Control daemon",
        f"/bin/zsh {VAULT}/scripts/claude_remote_control.sh",
        kind="simple", restart="always", restart_sec=30,
        extra_lines=["StandardInput=data", "StandardInputText=y\\n"])
    # Alive-but-dead-poll-loop guard (2026-09-12, CLI 2.1.268): an archived session
    # leaves the daemon "returning to idle" and never polling again — process up,
    # host offline. See scripts/claude_remote_control_watchdog.py.
    u["claude-remote-control-watchdog.service"] = service(
        "claude-remote-control-watchdog", "Claude Remote Control poll-loop watchdog",
        f"/usr/bin/python3 {VAULT}/scripts/claude_remote_control_watchdog.py", cwd=HOME)
    u["claude-remote-control-watchdog.timer"] = timer(
        "claude-remote-control-watchdog", "Claude Remote Control poll-loop watchdog", every="5min")

    # -- macp-browser: the logged-in Chrome that console automation attaches to (CDP) --
    # Boot-time start only; Chrome must never be killed by systemd, only by
    # `browser.sh stop` (SIGTERM, so the profile's cookie DB closes cleanly).
    #   * `browser.sh up` starts Xvfb + Chrome with `setsid nohup ... &` and returns.
    #     setsid does NOT leave the unit's cgroup, so under the default
    #     KillMode=control-group a stop/restart would SIGTERM+SIGKILL Chrome along
    #     with the cgroup (the same trap that killed long_jobs on runner restarts).
    #   * RemainAfterExit=yes keeps the unit active after `up` exits, so its exit
    #     does not trigger a cgroup cleanup.
    #   * KillMode=process (not control-group, not the deprecated `none`): on
    #     stop/restart systemd signals only the already-exited main process, so the
    #     only path that ends Chrome is ExecStop = `browser.sh stop` (VM shutdown too).
    #   * No Restart=, no timer, and deliberately absent from daemonctl.UNITS and
    #     worker.LONG_LIVED_LABELS: maintenance's code-change restarts never touch it.
    #     A Chrome dying mid-day is browser_watch's job to report.
    #   * ConditionPathExists: `--enable` is a clean skip on hosts without macp-browser.
    #   * If a Chrome started by hand is already up, `up` leaves it alone (pgrep), but
    #     a later stop of this unit still stops it through `browser.sh stop`.
    u["macp-browser.service"] = service(
        "macp-browser", "macp-browser logged-in Chrome (Xvfb :77, CDP 127.0.0.1:9277) at boot",
        f"/bin/bash {BROWSER_DIR}/browser.sh up", cwd=BROWSER_DIR,
        unit_lines=[f"ConditionPathExists={BROWSER_DIR}/browser.sh"], install=True,
        extra_lines=["RemainAfterExit=yes", f"ExecStop=/bin/bash {BROWSER_DIR}/browser.sh stop",
                     "KillMode=process",
                     "# Chrome is stopped only via ExecStop (browser.sh stop, SIGTERM): setsid",
                     "# does not leave this cgroup, so KillMode=process + RemainAfterExit keep",
                     "# systemd from killing the logged-in profile on stop/restart."])

    # -- mailroom --
    u["mailroom-tick.service"] = service(
        "mailroom-tick", "mailroom tick", f"{UV} run --directory {MAILROOM} mailroom tick", cwd=MAILROOM)
    u["mailroom-tick.timer"] = timer("mailroom-tick", "mailroom tick", every="15min")
    # No digest unit: the 21:30 kakao digest was retired 2026-09-11 — brief.sh
    # now embeds `mailroom digest --out` into the 08:30 morning brief.

    # -- KIS paper trading (모의, BUY_AND_HOLD) --
    u["kis-trading.service"] = service(
        "kis-trading", "KIS trading loop (paper)", f"{py} trading_loop.py",
        cwd=TRADING, kind="simple", restart="on-failure", restart_sec=60, extra_env=KIS_ENV_FILE)
    # -- KIS real trading (실계좌) — INSTALL-ONLY, never auto-enabled (see INSTALL_ONLY) --
    # Same lifecycle as the paper loop (simple / on-failure / 60s, no OOMPolicy) but
    # runs from its own clone + dedicated venv, and supplies the four trading params
    # INLINE. Credentials live in that clone's .secrets/kis.env (loaded by the code,
    # NOT wired here as an EnvironmentFile). That kis.env carries KIS_ENV/creds but
    # NOT BUY_AND_HOLD/KIS_MAX_DEPLOY_KRW — so without these inline the live account
    # would run on momentum at unbounded size. A human arms this unit by hand once.
    u["kis-trading-real.service"] = service(
        "kis-trading-real",
        "KIS trading loop (real account — install-only, human-enabled)",
        f"{TRADING_REAL}/.venv/bin/python trading_loop.py",
        cwd=TRADING_REAL, kind="simple", restart="on-failure", restart_sec=60,
        extra_lines=["Environment=KIS_ENV=real", "Environment=BUY_AND_HOLD=1",
                     "Environment=KIS_UNIVERSE=etf", "Environment=KIS_MAX_DEPLOY_KRW=3000000",
                     # The code's default alert path is the pre-rename second-brain vault;
                     # without this the real loop's Kakao alerts fail rc=127 (paper gets it
                     # from ~/.config/macp/kis.env, which this unit deliberately skips).
                     f"Environment=KIS_ALERT_SCRIPT={VAULT}/scripts/kakao_send.sh",
                     # Route this unit's Slack sibling-send to the dedicated kis channel
                     # (real account skips kis.env, so it can't inherit this line).
                     "Environment=NOTIFY_CHANNEL_KEY=kis"])
    u["kis-trading-livesync.service"] = service(
        "kis-trading-livesync", "KIS live sync", f"{py} scripts/live_sync.py",
        cwd=TRADING, extra_env=KIS_ENV_FILE)
    u["kis-trading-livesync.timer"] = timer(
        "kis-trading-livesync", "KIS live sync", calendar=["*-*-* 16:10:00", "*-*-* 21:10:00"])
    u["kis-trading-watchdog.service"] = service(
        "kis-trading-watchdog", "KIS trading watchdog", f"{py} watchdog.py",
        cwd=TRADING, extra_env=KIS_ENV_FILE)
    u["kis-trading-watchdog.timer"] = timer("kis-trading-watchdog", "KIS trading watchdog", every="2min")
    return u


def _backfill_identity(path: Path) -> None:
    """Append the identity keys an env file written before they existed lacks.

    write_env() never rewrites an existing env file (it holds hand-filled
    secrets), so a host installed before MACP_LABEL_PREFIX was added kept running
    without it: units with WorkingDirectory=$HOME cannot see .macp/config.toml,
    fell back to the neutral com.macp. prefix, and daemonctl could no longer map
    the host's pinned labels to units (claude-upgrade failed to restart remote-control,
    2026-09-26). Only missing keys with a known value are appended."""
    present = {ln.split("=", 1)[0].strip() for ln in path.read_text().splitlines()
               if "=" in ln and not ln.lstrip().startswith("#")}
    add = [f"{k}={v}" for k, v in (("MACP_LABEL_PREFIX", LABEL_PREFIX),
                                   ("MACP_CALENDAR_ID", CALENDAR_ID))
           if v and k not in present]
    if add:
        with path.open("a") as f:
            f.write("\n".join(add) + "\n")
        print(f"backfilled {path}: {', '.join(a.split('=')[0] for a in add)}")


def write_env() -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    for path, text in ((ENV_FILE, ENV_TEMPLATE), (KIS_ENV_FILE, KIS_ENV_TEMPLATE)):
        if path.exists():
            print(f"keep {path}")
            if path == ENV_FILE:
                _backfill_identity(path)
            continue
        path.write_text(text)
        path.chmod(0o600)
        print(f"wrote {path} (fill FILL_ME values)")
    if DASHBOARD_ENV_FILE.exists():
        print(f"keep {DASHBOARD_ENV_FILE}")
    else:
        DASHBOARD_ENV_FILE.write_text(f"MACP_DASHBOARD_TOKEN={secrets.token_urlsafe(24)}\n")
        DASHBOARD_ENV_FILE.chmod(0o600)
        print(f"wrote {DASHBOARD_ENV_FILE} (random dashboard token)")


def enable_targets(units: dict[str, str], only: list[str] | None) -> list[str]:
    """Unit file names to ``systemctl enable --now`` for, given an optional
    ``--only`` stem filter. INSTALL_ONLY stems are dropped FIRST — before the
    ``--only`` filter — so no invocation (not even ``--enable --only
    kis-trading-real``) can start them; those are armed by a human by hand."""
    targets: list[str] = []
    for name in units:
        stem, kind = name.rsplit(".", 1)
        if stem in INSTALL_ONLY:
            continue
        if only is not None and stem not in only:
            continue
        if kind == "timer" or f"{stem}.timer" not in units:
            targets.append(name)   # timers, and services with no timer (simple daemons)
    return targets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="dry run: print units to stdout")
    ap.add_argument("--enable", action="store_true", help="daemon-reload + enable --now timers/services")
    ap.add_argument("--only", nargs="*", default=None, help="unit stems to enable (default: all)")
    ap.add_argument("--write-env", action="store_true", help="create env files from templates if missing")
    ap.add_argument("--engine-python", default=None, metavar="PATH",
                    help="installed mode: absolute interpreter of the installed engine "
                         "(default: env MACP_ENGINE_PYTHON; empty = off)")
    args = ap.parse_args(argv)

    try:
        units = build(args.engine_python)
    except ValueError as e:
        ap.error(str(e))
    if args.print:
        for name, text in units.items():
            print(f"##### {name}\n{text}")
        return 0
    if args.write_env:
        write_env()
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    for name, text in units.items():
        (UNIT_DIR / name).write_text(text)
    print(f"wrote {len(units)} units to {UNIT_DIR}")
    if not args.enable:
        return 0
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    targets = enable_targets(units, args.only)
    proc = subprocess.run(["systemctl", "--user", "enable", "--now", *targets],
                          capture_output=True, text=True)
    print(proc.stdout or proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
