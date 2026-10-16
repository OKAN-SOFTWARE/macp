#!/usr/bin/env python3
"""Restart the dashboard's dev tunnel host when the process is alive but no longer hosting.

Installed on the cloud HQ as devtunnel-watchdog.timer (every 5 min), next to the
hand-installed macp-devtunnel.service — see docs/remote-access.md.

Observed 2026-09-24 15:27 KST: the relay dropped the host's websocket, the host
tried to refresh its tunnel access token, got `401 Refreshed tunnel access token
is not valid`, and then just sat there. The process stayed alive, so systemd's
`Restart=always` never fired, and for ~14 h the public URL answered nothing — the
Android app showed "서버에 연결할 수 없습니다". The GitHub login behind the CLI was
still valid: `systemctl --user restart macp-devtunnel` alone fixed it.

Signal: `devtunnel show <tunnel>` → `Host connections : N`, the relay service's own
count of hosts attached to the tunnel. It is state, not a log grep.

  - N ≥ 1                         → healthy (clears any pending verdict).
  - N = 0 on two consecutive ticks → restart the unit (one tick of 0 can be a
    normal reconnect in progress; a host younger than MIN_UPTIME_S is left alone).
  - still 0 within GRACE_S of our own restart → a restart does not help; alert once.
  - `devtunnel show` fails twice in a row (CLI login expired, network) → a restart
    cannot fix that either; alert once with the re-login command.
  - unit not active → a human stopped it; do nothing.

  --dry-run   report the verdict, mutate nothing (state file untouched).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_auth_watch import notify  # noqa: E402  (path shim must come first)

TUNNEL = os.environ.get("MACP_DEVTUNNEL_ID", "macp-dash")
UNIT = "macp-devtunnel.service"
DEVTUNNEL_BIN = Path(os.environ.get("MACP_DEVTUNNEL_BIN", Path.home() / "bin" / "devtunnel"))
STATE_PATH = Path.home() / ".local" / "state" / "macp" / "devtunnel-watchdog.json"

MIN_UPTIME_S = 120      # a freshly started host is still connecting to the relay
GRACE_S = 15 * 60       # still dead this soon after our restart = restarting doesn't help

HOST_RE = re.compile(r"^Host connections\s*:\s*(\d+)\s*$", re.M)


def parse_host_connections(text: str) -> int | None:
    """`Host connections : N` from `devtunnel show`, or None if absent."""
    m = HOST_RE.search(text or "")
    return int(m.group(1)) if m else None


def verdict(hosts: int | None, active: bool, uptime_s: float | None, state: dict,
            now: float) -> str:
    """One of: ok · inactive · starting · suspect · restart · stuck · query_failed · unknown.

    `state` carries the previous tick: {"zero_since", "query_fail_since", "restarted_at"}.
    `unknown` = the first failed query (wait one tick before calling it)."""
    if not active:
        return "inactive"
    if hosts is None:
        return "query_failed" if state.get("query_fail_since") is not None else "unknown"
    if hosts > 0:
        return "ok"
    if uptime_s is not None and uptime_s < MIN_UPTIME_S:
        return "starting"
    if state.get("zero_since") is None:
        return "suspect"
    restarted = state.get("restarted_at")
    if restarted is not None and now - restarted < GRACE_S:
        return "stuck"
    return "restart"


def next_state(v: str, state: dict, now: float) -> dict:
    """The state to persist after acting on verdict `v`.

    A healthy tick clears everything, so a later outage is a fresh incident. A
    restart keeps `stuck_alerted`: while restarts keep failing we retry every
    GRACE_S but alert only once, until the host is seen healthy again."""
    if v in ("ok", "inactive"):
        return {}
    s = dict(state)
    if v in ("unknown", "query_failed"):
        s.setdefault("query_fail_since", now)
        return s
    s.pop("query_fail_since", None)
    s.pop("query_alerted", None)
    if v == "suspect":
        s["zero_since"] = now
    elif v == "restart":
        # zero_since = now: a 0 on the very next tick is judged against our restart.
        s["restarted_at"] = now
        s["zero_since"] = now
    return s            # starting / stuck: unchanged


# ---- side effects ------------------------------------------------------------

def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def query_hosts() -> int | None:
    r = _run([str(DEVTUNNEL_BIN), "show", TUNNEL])
    if r is None or r.returncode != 0:
        return None
    return parse_host_connections(r.stdout)


def unit_active() -> bool:
    r = _run(["systemctl", "--user", "is-active", UNIT])
    return r is not None and r.stdout.strip() == "active"


def unit_uptime_s() -> float | None:
    """Seconds since the unit's main process started (systemd monotonic clock)."""
    r = _run(["systemctl", "--user", "show", UNIT, "-p",
              "ExecMainStartTimestampMonotonic", "--value"])
    try:
        usec = int((r.stdout if r else "").strip())
    except ValueError:
        return None
    return time.clock_gettime(time.CLOCK_MONOTONIC) - usec / 1e6 if usec else None


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def log(msg: str) -> None:
    print(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = time.time()
    state = load_state()
    hosts = query_hosts()
    active = unit_active()
    uptime = unit_uptime_s()
    v = verdict(hosts, active, uptime, state, now)
    log(f"tunnel={TUNNEL} hosts={hosts} active={active} "
        f"uptime={None if uptime is None else int(uptime)}s verdict={v}")
    if args.dry_run:
        return 0

    new = next_state(v, state, now)
    if v == "restart":
        r = _run(["systemctl", "--user", "restart", UNIT], timeout=60)
        log(f"restarted {UNIT} rc={None if r is None else r.returncode}")
    elif v == "stuck" and not state.get("stuck_alerted"):
        notify("macp 대시보드 터널 복구 실패",
               f"{UNIT}를 재시작했지만 {GRACE_S // 60}분이 지나도 dev tunnel 호스트가 붙지 않습니다. "
               "앱·공개 URL로 대시보드에 접속할 수 없습니다. VM에서 "
               f"`journalctl --user -u {UNIT}`와 `devtunnel show {TUNNEL}`를 확인하세요.")
        new["stuck_alerted"] = True
    elif v == "query_failed" and not state.get("query_alerted"):
        notify("macp 대시보드 터널 조회 실패",
               f"`devtunnel show {TUNNEL}`가 연속 실패합니다 — devtunnel CLI 로그인이 만료됐을 수 "
               "있습니다. 재시작으로는 고쳐지지 않으니 VM에서 `~/bin/devtunnel user login -g -d` 후 "
               f"`systemctl --user restart {UNIT}`.")
        new["query_alerted"] = True
    save_state(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
