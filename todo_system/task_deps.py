"""Task-level dependencies — a blocked task auto-activates once every task it
declares in `depends_on` is done. The task-level companion to the project-level
graph in `macp/deps.py` (GitHub issue #21).

Todo-item frontmatter fields:
    depends_on: [slug, ...]          # upstream tasks that must be done first
    on_unblock: auto|delegate|quick|discover|review|manual   # mode to flip to (default: auto)
    auto_activate: false             # opt OUT of auto-flip (default: true)
    human_gate: true                 # a person must look before this runs, even
                                     # with every dep done — never auto-flipped

Two spellings of "blocked" exist in the vault and both are honoured:
    worker_mode: blocked   → always eligible; the flip rewrites worker_mode.
    status: blocked        → eligible only when `on_unblock:` is written out; the
                             flip rewrites status back to `open` *and* worker_mode.
Rewriting both lines is what makes the flip stick — leave status at `blocked` and
the task is re-reported on every pass forever.

Design note: matches the vault philosophy that the human decides *project* stage
transitions (create_decision_gates is deliberately human-only). This only chains
**pure code tasks** the author explicitly wired with `depends_on`; anything
operational stays safe with `auto_activate: false`, or by leaving `on_unblock`
off a status-blocked task, or `worker_mode: manual` on one that is merely open.
Todo/items/*.md is the source of truth, so we flip the md and let import-md sync.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from pathlib import Path

from .cli import DB_PATH, ITEMS_DIR, parse_frontmatter

# `review` = run nothing until a human looks (not in DISPATCHABLE_MODES). It was
# missing here, and an unknown value fell through to "auto" — so
# `on_unblock: review` on play-store-7 (2026-09-11) auto-dispatched the one
# leaf its author had gated on a person. Unknown values now fall to `manual`:
# the safe direction for a field whose whole job is to say how much autonomy.
VALID_MODES = ("auto", "delegate", "quick", "discover", "review", "manual")
UNKNOWN_MODE_FALLBACK = "manual"
_WM_RE = re.compile(r"^worker_mode:.*$", re.M)
# count=1 + a column-0 anchor is enough to stay inside the frontmatter: it sits at
# the top of the file, and a Progress Log entry is written as "- YYYY-MM-DD: ...".
_ST_RE = re.compile(r"^status:.*$", re.M)


def _clean(x: str) -> str:
    # strip quotes, then wikilink brackets (`[[slug]]`) and an optional `|alias`
    # suffix (`[[slug|alias]]`) — depends_on is written in the same `[[slug]]`
    # convention as `parent:`, and must resolve to the bare slug to match done().
    s = str(x).strip().strip('"').strip("'").strip()
    s = s.strip("[]").strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    return s


def _as_list(v) -> list[str]:
    """Accept a YAML block list (already a list), or an inline `[a, b]` / `a, b`
    string (cli.parse_frontmatter returns inline lists as a raw string)."""
    if isinstance(v, list):
        return [_clean(x) for x in v if _clean(x)]
    if isinstance(v, str) and v.strip():
        return [_clean(x) for x in v.strip().strip("[]").split(",") if _clean(x)]
    return []


def _as_bool(v, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() not in ("false", "no", "0", "off")


def scan_items(items_dir: Path | str = ITEMS_DIR) -> dict[str, dict]:
    """{slug: {status, worker_mode, depends_on, on_unblock, auto_activate,
    human_gate, path}}."""
    tasks: dict[str, dict] = {}
    d = Path(items_dir)
    if not d.exists():
        return tasks
    for p in d.glob("*.md"):
        try:
            fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        # strip any inline "# comment" — the frontmatter parser doesn't, and a stray
        # comment must not silently downgrade the safety-critical on_unblock mode.
        # An absent/blank field parses to "" (a bare `on_unblock:` yields []), which
        # keeps `on_unblock_explicit` false — that flag is what admits a status-blocked
        # task, so it must mean "the author wrote a value", not "we defaulted one".
        raw_unblock = str(fm.get("on_unblock") or "").split("#")[0].strip().lower()
        # Same inline-comment strip as on_unblock (the frontmatter parser leaves
        # `# ...` on the value): `human_gate: true  # 사람 확인` must read True, not
        # downgrade to False. Default False — only a task that WRITES the flag is
        # protected, so no existing blocked+deps-done task changes behaviour.
        raw_gate = str(fm.get("human_gate") or "").split("#")[0].strip()
        tasks[p.stem] = {
            "path": p,
            "status": str(fm.get("status", "")).strip().lower(),
            "worker_mode": str(fm.get("worker_mode", "")).strip().lower(),
            "depends_on": _as_list(fm.get("depends_on")),
            "on_unblock": (raw_unblock if raw_unblock in VALID_MODES
                           else ("auto" if not raw_unblock else UNKNOWN_MODE_FALLBACK)),
            "on_unblock_explicit": bool(raw_unblock),
            "auto_activate": _as_bool(fm.get("auto_activate"), True),
            "human_gate": _as_bool(raw_gate, False) if raw_gate else False,
        }
    return tasks


def _is_blocked(t: dict) -> bool:
    """Whether a task is dependency-blocked. Two shapes exist in the vault:

    - `worker_mode: blocked` — always eligible (the mode says nothing but "waiting").
    - `status: blocked` — eligible only with an explicit `on_unblock:`. Such a task
      keeps a real worker_mode of its own, so without that field we cannot tell
      "waiting on a dep" from "operational, parked by a human"; requiring it keeps
      the opt-in deliberate instead of flipping parked work to the `auto` default.
    """
    if t["worker_mode"] == "blocked":
        return True
    return t["status"] == "blocked" and t.get("on_unblock_explicit", False)


def _in_cycle(slug: str, tasks: dict[str, dict]) -> bool:
    """True if `slug` participates in a depends_on cycle (DFS with a path stack)."""
    path: set[str] = set()

    def visit(s: str) -> bool:
        if s in path:
            return True
        path.add(s)
        for up in tasks.get(s, {}).get("depends_on", []):
            if visit(up):
                return True
        path.discard(s)
        return False

    return visit(slug)


def _db_status(db: Path | str) -> dict[str, str]:
    m: dict[str, str] = {}
    try:
        con = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
        for slug, status in con.execute("SELECT slug, status FROM tasks"):
            m[slug] = (status or "").lower()
        con.close()
    except Exception:
        pass
    return m


def resolve_unblocks(tasks: dict[str, dict], db: Path | str = DB_PATH
                     ) -> tuple[list[tuple[str, str]], list[str]]:
    """(flips, cyclic) — flips = [(slug, new_mode)] for blocked tasks whose every
    dependency is done and that aren't opted out; cyclic = slugs skipped for cycles.
    A dependency counts as done if its md OR the db says so (a done+archived upstream
    still unblocks)."""
    dbstat = _db_status(db)

    def done(dep: str) -> bool:
        return tasks.get(dep, {}).get("status") == "done" or dbstat.get(dep) == "done"

    flips, cyclic = [], []
    for slug, t in tasks.items():
        if not _is_blocked(t) or not t["depends_on"]:
            continue
        if not t["auto_activate"]:                 # explicit opt-out
            continue
        if t.get("human_gate"):
            # A person must look before this runs — even with every dep done. The
            # deps-done auto-flip is exactly what the 2026-10-12 incident rode: a
            # delegate PR closed `done` out from under a reviewer let task_deps
            # auto-activate a leaf its author had gated on a human. Scoped to the
            # flip decision only (not _is_blocked / dispatch eligibility), so the
            # task stays visible as blocked and a human still unblocks it by hand.
            continue
        if _in_cycle(slug, tasks):
            cyclic.append(slug)
            continue
        if all(done(d) for d in t["depends_on"]):
            flips.append((slug, t["on_unblock"]))
    return flips, cyclic


def apply_unblocks(items_dir: Path | str = ITEMS_DIR, db: Path | str = DB_PATH
                   ) -> tuple[list[tuple[str, str]], list[str]]:
    """Flip eligible blocked tasks in place (rewrite worker_mode + progress note).
    A status-blocked task also has its `status:` put back to `open`, without which
    the flip would not stick and the resolver would re-report it every cycle.
    Returns (flipped, cyclic-skipped). Caller runs import-md to sync the db."""
    tasks = scan_items(items_dir)
    flips, cyclic = resolve_unblocks(tasks, db)
    today = dt.date.today().isoformat()
    for slug, mode in flips:
        p = tasks[slug]["path"]
        s = p.read_text(encoding="utf-8")
        deps = ", ".join(tasks[slug]["depends_on"])
        was_status_blocked = tasks[slug]["status"] == "blocked"
        s2, n = _WM_RE.subn(f"worker_mode: {mode}", s, count=1)
        if was_status_blocked:
            # No worker_mode line to rewrite? Add one under status — leaving it out
            # reads as `off`, which would void the very flip we just decided on.
            repl = "status: open" if n else f"status: open\nworker_mode: {mode}"
            s2, n_status = _ST_RE.subn(repl, s2, count=1)
            n += n_status
        if n == 0:
            continue                               # neither line present — leave it
        flipped = "status open, worker_mode" if was_status_blocked else "worker_mode"
        note = f"\n- {today}: auto-unblocked (deps done: {deps}) → {flipped} {mode}"
        if "## Progress Log" in s2:
            s2 = s2.replace("## Progress Log\n", f"## Progress Log\n{note}\n", 1)
        else:
            s2 = s2.rstrip() + f"\n\n## Progress Log\n{note}\n"
        p.write_text(s2, encoding="utf-8")
    return flips, cyclic


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="task-deps",
                                 description="auto-activate blocked tasks whose deps are done")
    ap.add_argument("--dry-run", action="store_true", help="report only, don't flip")
    ap.add_argument("--items", default=str(ITEMS_DIR))
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args(argv)
    if a.dry_run:
        flips, cyclic = resolve_unblocks(scan_items(a.items), a.db)
    else:
        flips, cyclic = apply_unblocks(a.items, a.db)
    for slug, mode in flips:
        print(f"{'would unblock' if a.dry_run else 'unblocked'}: {slug} → worker_mode {mode}")
    for slug in cyclic:
        print(f"⚠ skipped (dependency cycle): {slug}")
    if not flips and not cyclic:
        print("no blocked tasks ready to activate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
