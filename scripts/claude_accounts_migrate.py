#!/usr/bin/env python3
"""One-shot migration of the legacy single Claude config dir into the shared
layout of ``todo_system/macp/accounts.py`` (``macp-multi-claude-account`` leaf ②).

What it does
------------
The pre-layout CLI kept everything in one directory — ``accounts.LEGACY_CONFIG_DIR``
— with the settings/auth-metadata file (``accounts.LEGACY_CLAUDE_JSON``) beside
it. This script moves that directory into leaf ①'s layout exactly once:

* every :data:`accounts.SHARED_ENTRIES` item → ``<shared_root>/<entry>``
* the account dir ``<accounts_root>/<account>`` (default ``school``) gets the
  nine layout symlinks, then everything else that was in the legacy dir
  (credentials, automation token, CLI caches) is moved into it
* the legacy settings file → inside the account dir
* the legacy dir and the legacy settings file become **symlinks** to the
  account side, so every consumer that still spells the legacy path (runner,
  usage guard, auth watch, hook paths in settings) keeps resolving until
  leaf ⑤ switches them over — do not remove those links before that
* relative ``projects/*/memory`` links are re-pointed to absolute targets
  (a relative target breaks the moment the link moves)
* profile ``default`` gains ``[[account]] school`` (priority 10) and
  ``active_account = "school"``. An existing profile is loaded and extended,
  never replaced, so its ``daily_run_budget`` survives

Safety rules
------------
* **dry-run by default** — without ``--apply`` nothing is changed; the plan is
  printed and the exit code is 0. ``--apply`` refuses (rc 3) while any of the
  daemons that use the config dir is active, unless ``--i-stopped-daemons``.
* a manifest ``<accounts_root>/_migration-<ts>/manifest.json`` is written
  **before** the first move and rewritten after every step; ``--rollback
  <manifest>`` undoes the ``done`` steps in reverse order.
* moves only: ``os.replace`` (same filesystem, checked up front). Nothing is
  deleted — the emptied legacy dir is ``rmdir``'d, and ``unlink`` touches only
  symlinks this script created itself.
* no secret reaches stdout, the log or the manifest: files are described by
  path and byte count only, and no file content is read except the profile
  (paths and numbers).

Exit codes: 0 ok / already migrated, 1 failed mid-way (manifest kept), 2
precheck (nothing to migrate, conflicts, bad arguments), 3 daemons active.

stdlib + ``todo_system.macp.accounts`` / ``config`` / ``todo_system.daemonctl``
only, so a bare ``python3`` from a vault checkout runs it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

_INSTALL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_INSTALL_DIR))          # bootstrap — raw install location
from todo_system import daemonctl  # noqa: E402
from todo_system.macp import accounts as ACC  # noqa: E402
from todo_system.macp import config as C  # noqa: E402

MANIFEST_VERSION = 1
DEFAULT_ACCOUNT = "school"
DEFAULT_PROFILE = "default"
DEFAULT_PRIORITY = 10

RC_OK = 0
RC_FAILED = 1
RC_PRECHECK = 2
RC_DAEMONS = 3

# Daemon units are assembled from the daemonctl label constants only, so the
# installed label prefix never appears in this file; the unit stems are
# prefix-free ("macp-…" / "claude-…").
_SB = daemonctl.SECONDBRAIN_PREFIX
_ORG = daemonctl.LABEL_PREFIX


def _unit(label: str, kind: str = "service") -> str:
    return f"{daemonctl.unit_name(label)}.{kind}"


# Hard gate: units that read the config dir. The orchestrator and auth-watch are
# timer-driven oneshots — between ticks their .service is "inactive" — so their
# .timer is checked too.
HARD_UNITS = (
    _unit(f"{_SB}runner.worker-default"),
    _unit(f"{_SB}orchestrator"),
    _unit(f"{_SB}orchestrator", "timer"),
    _unit(f"{_ORG}claude-remote-control"),
    _unit(f"{_ORG}claude-auth-watch"),
    _unit(f"{_ORG}claude-auth-watch", "timer"),
)
# Recommended to stop for the window as well (they call the CLI or restart
# daemons on their own schedule); never a hard condition.
ADVISORY_UNITS = (
    _unit(f"{_SB}maintenance", "timer"),
    _unit(f"{_SB}planner", "timer"),
    _unit(f"{_ORG}claude-upgrade", "timer"),
    _unit(f"{_ORG}claude-remote-control-watchdog", "timer"),
)

_OP_LABEL = {"rmdir-legacy": "rmdir"}


class UnexpectedState(RuntimeError):
    """The tree no longer matches the manifest — a human has to look (rc 2)."""


# ---- small helpers ------------------------------------------------------------

def _abs(p: Path | str) -> Path:
    """Expand ``~`` and make absolute without following symlinks (same meaning as
    the layout module's private helper; kept local so no private name is used)."""
    return Path(os.path.abspath(os.path.expanduser(str(p))))


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _present(p: Path) -> bool:
    """``exists()`` is False for a dangling link — count the link itself."""
    return p.is_symlink() or p.exists()


def _kind(p: Path) -> str:
    if p.is_symlink():
        return "symlink"
    if p.is_dir():
        return "dir"
    return "file"


def _move_entry(src: Path, dst: Path) -> dict:
    e: dict = {"op": "move", "src": str(src), "dst": str(dst), "kind": _kind(src),
               "done": False}
    if e["kind"] == "file":
        e["bytes"] = os.lstat(src).st_size            # size only — never the content
    elif e["kind"] == "symlink":
        e["link_target"] = os.readlink(src)
    return e


def _nearest_existing(p: Path) -> Path:
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _subject(e: dict) -> str:
    return e.get("src") or e.get("path") or ""


def _describe(e: dict) -> str:
    """One line per step — paths, kinds and byte counts only."""
    op = e["op"]
    head = f"{_OP_LABEL.get(op, op):<8} "
    if op == "mkdir":
        return head + e["path"]
    if op == "move":
        tag = e["kind"]
        if "bytes" in e:
            tag += f", {e['bytes']} bytes"
        if "link_target" in e:
            tag += f", -> {e['link_target']}"
        return head + f"{e['src']} -> {e['dst']}  [{tag}]"
    if op == "layout":
        new = ", ".join(Path(d).name for d in e["new_dirs"]) or "none"
        return head + (f"{e['path']}  ({len(e['links'])} links -> {e['shared_root']}/*, "
                       f"new shared dirs: {new})")
    if op == "rmdir-legacy":
        return head + f"{e['path']}  (must be empty by then)"
    if op == "symlink":
        return head + f"{e['path']} -> {e['target']}"
    if op == "repoint":
        line = head + f"{e['path']}: {e['old_target']} -> {e['new_target']}"
        if e.get("skipped"):
            line += f"  (skipped: {e['skipped']})"
        return line
    if op == "profile":
        return head + (f"{e['path']}: {e['action']} account '{e['account']}' "
                       f"(claude_config_dir={e['claude_config_dir']}, priority {e['priority']}), "
                       f"active_account = \"{e['active_account']}\"")
    return head + json.dumps(e)


# ---- plan (pure read) -----------------------------------------------------------

def build_plan(account: str = DEFAULT_ACCOUNT, profile: str = DEFAULT_PROFILE) -> dict:
    """Inspect the tree and return the manifest body plus ``status`` (``ready`` |
    ``already_migrated`` | ``nothing_to_migrate`` | ``conflict``), ``conflicts``
    and ``warnings``. Reads only; every path is resolved at call time.
    """
    legacy = _abs(ACC.legacy_config_dir())
    legacy_json = _abs(ACC.legacy_claude_json())
    shared = _abs(ACC.resolve_shared_root())
    accounts_root = _abs(ACC.resolve_accounts_root())
    now = _utc_now()
    plan: dict = {
        "version": MANIFEST_VERSION,
        "created": now.isoformat(timespec="seconds"),
        "platform": sys.platform,
        "legacy_dir": str(legacy),
        "legacy_dir_mode": None,
        "legacy_claude_json": str(legacy_json),
        "legacy_claude_json_present": False,
        "shared_root": str(shared),
        "accounts_root": str(accounts_root),
        "account": account,
        "account_dir": "",
        "profile_name": profile,
        "profile_path": str(C.PROFILES / profile / "config.toml"),
        "profile_before": None,
        "manifest_path": str(accounts_root / f"_migration-{now.strftime('%Y%m%dT%H%M%SZ')}"
                             / "manifest.json"),
        "status": "ready",
        "conflicts": [],
        "warnings": [],
        "entries": [],
    }
    conflicts: list[str] = plan["conflicts"]
    warnings: list[str] = plan["warnings"]
    entries: list[dict] = plan["entries"]

    if sys.platform != "linux":
        warnings.append(
            f"platform '{sys.platform}' is not linux — the macOS login Keychain is not "
            "covered; whether the CLI keeps one credential per CLAUDE_CONFIG_DIR is "
            "unverified (docs/claude-accounts.md §5)")

    # 1. account name
    try:
        acct = _abs(ACC.account_dir(account))
    except ValueError as exc:
        conflicts.append(str(exc))
        plan["status"] = "conflict"
        return plan
    plan["account_dir"] = str(acct)
    acct_json = ACC.claude_json_path(acct)

    # 2. already migrated / 3. nothing to migrate
    if legacy.is_symlink():
        plan["status"] = "already_migrated"
        plan["already"] = {
            "legacy_dir_target": os.readlink(legacy),
            "legacy_claude_json_target": (os.readlink(legacy_json)
                                          if legacy_json.is_symlink() else None),
        }
        return plan
    if not legacy.is_dir():
        plan["status"] = "nothing_to_migrate"
        return plan
    plan["legacy_dir_mode"] = stat.S_IMODE(os.lstat(legacy).st_mode)

    # 4. conflicts — collected, not short-circuited
    if legacy_json.is_symlink():
        conflicts.append(
            f"legacy settings file is already a symlink: {legacy_json} -> "
            f"{os.readlink(legacy_json)} (partial earlier apply? run --rollback <manifest> first)")
    elif legacy_json.exists() and not legacy_json.is_file():
        conflicts.append(f"legacy settings file is not a regular file: {legacy_json}")
    json_present = legacy_json.is_file() and not legacy_json.is_symlink()
    plan["legacy_claude_json_present"] = json_present
    for what, root in (("shared root", shared), ("accounts root", accounts_root)):
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            conflicts.append(f"{what} is not a directory: {root}")
    if acct.is_symlink():
        conflicts.append(f"account dir is a symlink: {acct}")
    elif acct.exists():
        if not acct.is_dir():
            conflicts.append(f"account dir is not a directory: {acct}")
        elif any(acct.iterdir()):
            conflicts.append(f"account dir exists and is not empty: {acct} "
                             "(the layout is built in an empty dir only)")

    # step 1: shared root
    if not shared.is_dir():
        entries.append({"op": "mkdir", "path": str(shared), "done": False})
    # step 2: shared entries legacy -> shared
    for entry in ACC.SHARED_ENTRIES:
        src = legacy / entry
        if not _present(src):
            continue
        dst = shared / entry
        if _present(dst):
            conflicts.append(f"already exists: {dst} (would receive {src})")
        e = _move_entry(src, dst)
        if e["kind"] == "symlink" and not os.path.isabs(e["link_target"]):
            warnings.append(f"{src} is a symlink with a relative target "
                            f"({e['link_target']}); moved as-is — verify the target after migration")
        entries.append(e)
    # step 3: account dir + layout (the shared dirs the layout call will create)
    new_dirs = [str(shared / d) for d in ACC.SHARED_ENTRIES
                if d in ACC.SHARED_DIR_ENTRIES
                and not _present(legacy / d) and not _present(shared / d)]
    entries.append({
        "op": "layout", "path": str(acct), "shared_root": str(shared),
        "links": [{"path": str(acct / e), "target": str(shared / e),
                   "dir": e in ACC.SHARED_DIR_ENTRIES} for e in ACC.SHARED_ENTRIES],
        "new_dirs": new_dirs, "mode": plan["legacy_dir_mode"], "done": False,
    })
    # step 4: everything else legacy -> account dir (hidden entries included)
    json_name = legacy_json.name
    for name in sorted(p.name for p in legacy.iterdir() if p.name not in ACC.SHARED_ENTRIES):
        src, dst = legacy / name, acct / name
        if _present(dst):
            conflicts.append(f"already exists: {dst} (would receive {src})")
        if name == json_name and json_present:
            conflicts.append(f"both {src} and {legacy_json} would land on {acct_json}")
        entries.append(_move_entry(src, dst))
    # step 5: legacy settings file -> account dir
    if json_present:
        if _present(acct_json):
            conflicts.append(f"already exists: {acct_json} (would receive {legacy_json})")
        entries.append(_move_entry(legacy_json, acct_json))
    # steps 6-8: empty legacy dir -> symlink(s)
    entries.append({"op": "rmdir-legacy", "path": str(legacy),
                    "mode": plan["legacy_dir_mode"], "done": False})
    entries.append({"op": "symlink", "path": str(legacy), "target": str(acct),
                    "dir": True, "done": False})
    if json_present:
        entries.append({"op": "symlink", "path": str(legacy_json), "target": str(acct_json),
                        "dir": False, "done": False})
    # step 9: relative memory links, computed from the pre-move location
    projects = legacy / "projects"
    if projects.is_dir() and not projects.is_symlink():
        for key_dir in sorted(projects.iterdir()):
            link = key_dir / "memory"
            if not key_dir.is_dir() or not link.is_symlink():
                continue
            old = os.readlink(link)
            if os.path.isabs(old):
                continue
            new = str(link.resolve())
            e = {"op": "repoint", "path": str(shared / "projects" / key_dir.name / "memory"),
                 "old_target": old, "new_target": new, "done": False}
            if not Path(new).exists():
                e["skipped"] = "dangling"
                warnings.append(f"{link}: relative memory link is dangling "
                                f"({old} -> {new}); left as is")
            entries.append(e)
    # step 10: profile — loaded and extended, never replaced
    profile_path = Path(plan["profile_path"])
    if profile_path.is_file():
        try:
            plan["profile_before"] = profile_path.read_text(encoding="utf-8")
        except OSError as exc:
            conflicts.append(f"cannot read profile {profile_path}: {exc}")
    prof = C.load_profile(profile)
    if prof is None:
        action = "create"
        budget = C.Profile(name=profile).daily_run_budget
        if profile_path.exists():
            warnings.append(f"profile {profile_path} exists but holds no settings "
                            "(empty / comment-only / unparsable); it is rewritten with the "
                            f"defaults (daily_run_budget = {budget})")
        else:
            warnings.append(f"profile {profile_path} does not exist; it is created with the "
                            f"defaults (daily_run_budget = {budget})")
    else:
        action = "update" if any(a.name == account for a in prof.accounts) else "add"
        budget = prof.daily_run_budget
    entries.append({
        "op": "profile", "path": str(profile_path), "action": action, "account": account,
        "claude_config_dir": str(acct), "priority": DEFAULT_PRIORITY,
        "active_account": account, "daily_run_budget": budget, "done": False,
    })

    # same filesystem for every root — os.replace cannot cross devices
    devs: dict[str, int] = {}
    roots = [("legacy dir", legacy), ("shared root", shared), ("accounts root", accounts_root)]
    if json_present:
        roots.append(("legacy settings file", legacy_json))
    for what, p in roots:
        try:
            devs[what] = os.stat(_nearest_existing(p)).st_dev
        except OSError as exc:
            conflicts.append(f"cannot stat {what} {p}: {exc}")
    if len(set(devs.values())) > 1:
        conflicts.append("cross-device: " + ", ".join(f"{k}: {v}" for k, v in devs.items())
                         + " (os.replace cannot move across filesystems)")

    if conflicts:
        plan["status"] = "conflict"
    return plan


def print_plan(plan: dict, *, apply: bool) -> None:
    status = plan["status"]
    if status == "already_migrated":
        a = plan["already"]
        print(f"already migrated: {plan['legacy_dir']} -> {a['legacy_dir_target']}")
        if a["legacy_claude_json_target"]:
            print(f"already migrated: {plan['legacy_claude_json']} -> "
                  f"{a['legacy_claude_json_target']}")
        return
    if status == "nothing_to_migrate":
        print(f"nothing to migrate: {plan['legacy_dir']} (not a directory)")
        return
    mode = "apply" if apply else "dry-run (nothing is changed; add --apply after stopping the daemons)"
    print(f"claude_accounts_migrate — {mode}")
    m = plan["legacy_dir_mode"]
    print(f"  legacy dir:            {plan['legacy_dir']}"
          + (f"  [dir, mode {m:04o}]" if m is not None else ""))
    lj = Path(plan["legacy_claude_json"])
    if plan["legacy_claude_json_present"]:
        print(f"  legacy settings file:  {lj}  [file, {os.lstat(lj).st_size} bytes]")
    else:
        print(f"  legacy settings file:  {lj}  [absent]")
    print(f"  shared root:           {plan['shared_root']}")
    print(f"  accounts root:         {plan['accounts_root']}")
    print(f"  account:               {plan['account']} -> {plan['account_dir'] or '?'}")
    print(f"  manifest:              {plan['manifest_path']}")
    prof_entries = [e for e in plan["entries"] if e["op"] == "profile"]
    if prof_entries:
        e = prof_entries[0]
        print(f"  profile:               {e['path']}: {e['action']} account '{e['account']}' "
              f"(claude_config_dir={e['claude_config_dir']}, priority {e['priority']}), "
              f"active_account = \"{e['active_account']}\"")
        if e["action"] == "create":
            print(f"                         — file is created by write_profile "
                  f"(daily_run_budget = {e['daily_run_budget']}, the default)")
        else:
            print(f"                         — file is rewritten by write_profile: comments "
                  f"dropped, daily_run_budget = {e['daily_run_budget']} preserved")
    if plan["entries"]:
        print(f"plan ({len(plan['entries'])} steps):")
        for i, e in enumerate(plan["entries"], 1):
            print(f"  {i:2d}. {_describe(e)}")
    if plan["warnings"]:
        print("warnings:")
        for w in plan["warnings"]:
            print(f"  - {w}")
    if plan["conflicts"]:
        print("conflicts (precheck failed — nothing was changed):")
        for c in plan["conflicts"]:
            print(f"  - {c}")


# ---- daemons ----------------------------------------------------------------------

def check_daemons() -> tuple[list[str], str | None]:
    """``(active_units, error)`` over :data:`HARD_UNITS` via ``systemctl --user
    is-active`` (rc 0 = active). A missing ``systemctl`` is reported as an error;
    any other failure counts the unit as active (fail closed)."""
    active: list[str] = []
    for unit in HARD_UNITS:
        try:
            cp = subprocess.run(["systemctl", "--user", "is-active", unit],
                                capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return [], "systemctl not found — cannot verify daemons"
        except (OSError, subprocess.SubprocessError):
            active.append(f"{unit} (unknown)")
            continue
        if cp.returncode == 0:
            active.append(unit)
    return active, None


def stop_hint(active: list[str]) -> str:
    hard = " ".join(HARD_UNITS)
    adv = " ".join(ADVISORY_UNITS)
    lines = []
    if active:
        lines.append("# active now: " + ", ".join(active))
    lines += [
        f"systemctl --user stop {hard}",
        f"# recommended for the window: systemctl --user stop {adv}",
        "# resume afterwards (runner last, only when no run is in_progress): "
        + _resume_hint(),
    ]
    return "\n".join(lines)


def _daemon_gate(verb: str, bypass: bool) -> int:
    if bypass:
        print("daemon check skipped (--i-stopped-daemons)")
        return RC_OK
    active, err = check_daemons()
    if err:
        print(f"cannot verify daemons ({err}) — stop them by hand and re-run with "
              "--i-stopped-daemons")
        return RC_DAEMONS
    if active:
        print(f"refusing to {verb}: active units: " + ", ".join(active))
        print(stop_hint(active))
        print("(or re-run with --i-stopped-daemons if you stopped them another way)")
        return RC_DAEMONS
    print("daemons: none of the hard units is active")
    return RC_OK


# ---- manifest ----------------------------------------------------------------------

def write_manifest(path: Path, plan: dict) -> None:
    """Atomic rewrite (tmp file + ``os.replace``). Holds paths, kinds, byte counts
    and the pre-migration profile text — never a file's content or a token."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_manifest(path: Path) -> dict:
    """Read and shape-check a manifest; ``ValueError`` when it is not one of ours."""
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read manifest {path}: {exc}") from None
    if not isinstance(plan, dict) or plan.get("version") != MANIFEST_VERSION:
        raise ValueError(f"not a version-{MANIFEST_VERSION} manifest: {path}")
    for key in ("legacy_dir", "legacy_claude_json", "shared_root", "accounts_root",
                "account", "account_dir", "profile_name", "profile_path", "entries"):
        if key not in plan:
            raise ValueError(f"manifest {path} lacks '{key}'")
    if not isinstance(plan["entries"], list):
        raise ValueError(f"manifest {path}: 'entries' is not a list")
    return plan


def _swap_symlink(p: Path, target: str) -> None:
    """Replace the symlink at ``p`` atomically: a new link beside it, then
    ``os.replace`` (rename swaps the link itself, never what it points to)."""
    tmp = p.with_name(p.name + ".macp-tmp")
    if _present(tmp):
        raise RuntimeError(f"temporary path exists: {tmp}")
    os.symlink(target, tmp)
    os.replace(tmp, p)


# ---- apply -----------------------------------------------------------------------------

def _exec(e: dict, plan: dict) -> None:
    op = e["op"]
    if op == "mkdir":
        Path(e["path"]).mkdir(parents=True, exist_ok=True)
    elif op == "move":
        src, dst = Path(e["src"]), Path(e["dst"])
        if _present(dst):
            raise RuntimeError(f"destination exists: {dst}")
        if not _present(src):
            raise RuntimeError(f"source missing: {src}")
        os.replace(src, dst)
    elif op == "layout":
        shared, acct = Path(e["shared_root"]), Path(e["path"])
        dir_entries = [shared / d for d in ACC.SHARED_ENTRIES if d in ACC.SHARED_DIR_ENTRIES]
        before = {str(d) for d in dir_entries if _present(d)}
        ACC.create_account_layout(plan["account"], accounts_root=plan["accounts_root"],
                                  shared_root=str(shared))
        drift = ACC.layout_drift(acct, str(shared))
        if drift:
            raise RuntimeError("layout drift right after create_account_layout: "
                               + "; ".join(drift))
        if e.get("mode") is not None:
            os.chmod(acct, e["mode"])
        created = [str(d) for d in dir_entries if str(d) not in before and _present(d)]
        if created != e["new_dirs"]:
            print(f"    note: shared dirs created by the layout: {', '.join(created) or 'none'} "
                  f"(planned: {', '.join(e['new_dirs']) or 'none'})")
            e["new_dirs"] = created
    elif op == "rmdir-legacy":
        p = Path(e["path"])
        if any(p.iterdir()):
            raise RuntimeError(f"legacy dir is not empty: {p} (left in place)")
        os.rmdir(p)
    elif op == "symlink":
        p = Path(e["path"])
        if _present(p):
            raise RuntimeError(f"path exists: {p}")
        p.symlink_to(e["target"], target_is_directory=e["dir"])
    elif op == "repoint":
        if e.get("skipped"):
            return
        p = Path(e["path"])
        if not p.is_symlink() or os.readlink(p) != e["old_target"]:
            raise RuntimeError(f"not the expected symlink: {p}")
        _swap_symlink(p, e["new_target"])
    elif op == "profile":
        name = plan["profile_name"]
        prof = C.load_profile(name) or C.Profile(name=name)
        for a in prof.accounts:
            if a.name == e["account"]:
                a.claude_config_dir = e["claude_config_dir"]
                a.priority = e["priority"]
                break
        else:
            prof.accounts.append(C.Account(name=e["account"],
                                           claude_config_dir=e["claude_config_dir"],
                                           priority=e["priority"]))
        prof.active_account = e["active_account"]
        C.write_profile(prof)
    else:
        raise RuntimeError(f"unknown op {op!r}")


def _resume_hint() -> str:
    units = " ".join((*ADVISORY_UNITS, *HARD_UNITS[1:], HARD_UNITS[0]))
    return f"systemctl --user start {units}"


def apply(plan: dict, manifest_path: Path) -> int:
    """Run the plan: manifest first, then each entry in order, rewriting the
    manifest (``done``) after every step. On failure the manifest is kept and
    the rollback command is printed (rc 1)."""
    manifest_path.parent.mkdir(parents=True, exist_ok=False)
    write_manifest(manifest_path, plan)
    print(f"manifest written: {manifest_path}")
    entries = plan["entries"]
    n = len(entries)
    for i, e in enumerate(entries, 1):
        try:
            _exec(e, plan)
        except Exception as exc:
            print(f"error at step {i}/{n} ({e['op']} {_subject(e)}): {exc!r}")
            print(f"manifest kept: {manifest_path} — "
                  f"python3 scripts/claude_accounts_migrate.py --rollback {manifest_path}")
            return RC_FAILED
        e["done"] = True
        write_manifest(manifest_path, plan)
        print(f"ok  {i:2d}/{n} {_describe(e)}")
    print(f"migration complete — manifest: {manifest_path}")
    print("next:")
    print("  1. run `claude` once interactively WITHOUT CLAUDE_CONFIG_DIR (the legacy path must")
    print("     resolve through the new symlink), then once after")
    print("     eval \"$(python -m todo_system.macp account env)\" — check /status in both")
    print("  2. start the units again (runner last, only when no run is in_progress):")
    print(f"     {_resume_hint()}")
    print("  3. human follow-ups: docs/claude-accounts.md §7.4")
    return RC_OK


# ---- rollback --------------------------------------------------------------------------

def _rmdir_if_empty(p: Path, what: str) -> None:
    if p.is_dir() and not p.is_symlink():
        if any(p.iterdir()):
            print(f"    note: {what} {p} left in place (not empty)")
        else:
            os.rmdir(p)


def _undo(e: dict, plan: dict) -> None:
    op = e["op"]
    if op == "mkdir":
        _rmdir_if_empty(Path(e["path"]), "shared root")
    elif op == "move":
        src, dst = Path(e["src"]), Path(e["dst"])
        if _present(src):
            raise UnexpectedState(f"original path exists again: {src}")
        if not _present(dst):
            raise UnexpectedState(f"moved item is missing: {dst}")
        os.replace(dst, src)
    elif op == "layout":
        for link in e["links"]:
            p = Path(link["path"])
            if p.is_symlink() and os.readlink(p) == link["target"]:
                p.unlink()
            elif _present(p):
                print(f"    note: {p} is not the layout link any more — left in place")
        _rmdir_if_empty(Path(e["path"]), "account dir")
        for d in e["new_dirs"]:
            _rmdir_if_empty(Path(d), "shared dir")
    elif op == "rmdir-legacy":
        p = Path(e["path"])
        if _present(p):
            raise UnexpectedState(f"legacy path exists: {p}")
        p.mkdir()
        if e.get("mode") is not None:
            os.chmod(p, e["mode"])
    elif op == "symlink":
        p = Path(e["path"])
        if p.is_symlink():
            cur = os.readlink(p)
            if cur != e["target"]:
                raise UnexpectedState(f"{p} points elsewhere: {cur} (expected {e['target']})")
            p.unlink()
        elif p.exists():
            raise UnexpectedState(
                f"{p} is no longer a symlink (a regular file/dir took its place) — merge it "
                f"into {e['target']} by hand, remove it, then re-run --rollback")
        else:
            print(f"    note: {p} already gone")
    elif op == "repoint":
        if e.get("skipped"):
            return
        p = Path(e["path"])
        if not p.is_symlink():
            raise UnexpectedState(f"{p} is not a symlink any more")
        cur = os.readlink(p)
        if cur == e["old_target"]:
            print(f"    note: {p} already restored")
        elif cur == e["new_target"]:
            _swap_symlink(p, e["old_target"])
        else:
            raise UnexpectedState(f"{p} points elsewhere: {cur}")
    elif op == "profile":
        return                                      # left as written; guidance at the end
    else:
        raise RuntimeError(f"unknown op {op!r}")


def _check_rollback_state(plan: dict) -> None:
    """The legacy dir must still be what the manifest says it made of it."""
    legacy = Path(plan["legacy_dir"])
    link_done = any(e["op"] == "symlink" and e["path"] == plan["legacy_dir"] and e.get("done")
                    for e in plan["entries"])
    if legacy.is_symlink():
        target = os.readlink(legacy)
        if target != plan["account_dir"]:
            raise UnexpectedState(f"{legacy} -> {target}, expected {plan['account_dir']}")
        if not link_done:
            raise UnexpectedState(f"{legacy} is a symlink but this manifest never created it")
    elif link_done and legacy.exists():
        raise UnexpectedState(f"{legacy} is no longer the symlink this migration created")


def rollback(manifest_path: Path, plan: dict | None = None) -> int:
    """Undo the ``done`` steps of a manifest in reverse order, rewriting the
    manifest after each one. The profile is left as written (guidance printed).
    rc 0 done / nothing to do, 2 unexpected state (manifest kept), 1 other error."""
    if plan is None:
        try:
            plan = load_manifest(manifest_path)
        except ValueError as exc:
            print(f"error: {exc}")
            return RC_PRECHECK
    try:
        _check_rollback_state(plan)
    except UnexpectedState as exc:
        print(f"unexpected state: {exc}")
        print(f"manifest kept: {manifest_path} — fix the tree by hand, then re-run --rollback")
        return RC_PRECHECK
    todo = [(i, e) for i, e in enumerate(plan["entries"], 1) if e.get("done")]
    if not todo:
        print(f"nothing to roll back: no step of {manifest_path} is marked done")
        return RC_OK
    n = len(plan["entries"])
    print(f"claude_accounts_migrate — rollback of {manifest_path} "
          f"({len(todo)} done steps, reverse order)")
    for i, e in reversed(todo):
        try:
            _undo(e, plan)
        except UnexpectedState as exc:
            print(f"unexpected state at undo {i}/{n} ({e['op']} {_subject(e)}): {exc}")
            print(f"manifest kept: {manifest_path} — fix the tree by hand, then re-run --rollback")
            return RC_PRECHECK
        except Exception as exc:
            print(f"error at undo {i}/{n} ({e['op']} {_subject(e)}): {exc!r}")
            print(f"manifest kept: {manifest_path} — re-run --rollback after fixing the cause")
            return RC_FAILED
        e["done"] = False
        write_manifest(manifest_path, plan)
        print(f"undo {i:2d}/{n} {_describe(e)}")
    print(f"rollback complete — manifest: {manifest_path} (every step is marked undone)")
    prof = [e for e in plan["entries"] if e["op"] == "profile"]
    if prof:
        if plan.get("profile_before") is None:
            print(f"profile: {prof[0]['path']} was created by the migration and is left as "
                  "written — delete it by hand if unwanted")
        else:
            print(f"profile: {prof[0]['path']} is left as written — restore it by hand from "
                  "the manifest's profile_before text")
    return RC_OK


# ---- cli ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="claude_accounts_migrate",
        description="Move the legacy single Claude config dir into the shared "
                    "layout (dry-run unless --apply).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true",
                   help="perform the migration (default: print the plan only)")
    g.add_argument("--rollback", metavar="MANIFEST",
                   help="undo a previous --apply from its manifest.json "
                        "(reverse order, done steps only)")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT,
                    help=f"account name for the legacy credentials (default {DEFAULT_ACCOUNT})")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help=f"profile that gets the [[account]] entry (default {DEFAULT_PROFILE})")
    ap.add_argument("--i-stopped-daemons", action="store_true", dest="i_stopped_daemons",
                    help="skip the systemctl is-active gate (only when you stopped the "
                         "units another way)")
    args = ap.parse_args(argv)

    if args.rollback:
        manifest_path = Path(args.rollback)
        try:
            plan = load_manifest(manifest_path)
        except ValueError as exc:
            print(f"error: {exc}")
            return RC_PRECHECK
        rc = _daemon_gate("roll back", args.i_stopped_daemons)
        if rc:
            return rc
        return rollback(manifest_path, plan)

    plan = build_plan(args.account, args.profile)
    print_plan(plan, apply=args.apply)
    if plan["status"] == "already_migrated":
        return RC_OK
    if plan["status"] != "ready":
        return RC_PRECHECK
    if not args.apply:
        return RC_OK
    rc = _daemon_gate("apply", args.i_stopped_daemons)
    if rc:
        return rc
    return apply(plan, Path(plan["manifest_path"]))


if __name__ == "__main__":
    sys.exit(main())
