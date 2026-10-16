"""`not_before` — the one place that decides whether a task is still deferred.

Accepted shapes (frontmatter or todo.db TEXT):

- `YYYY-MM-DD`                 — deferred until 00:00 that day (the original form)
- `YYYY-MM-DD HH:MM[:SS]`      — deferred until that time
- `YYYY-MM-DDTHH:MM[:SS]`      — same, ISO `T` separator

Times are host-local: compared against a naive `datetime.now()`. A date-only
value is 00:00 that day, so "deferred" is exactly the old `date > today`
check. `None`, empty, `-` and anything unparseable mean "no gate".

Every dispatch / NOW / stale / dashboard check calls `is_deferred`; tests pin
the clock by patching `_now`. Stdlib-only (the dashboard and the isolated
engine import it).
"""

from __future__ import annotations

import datetime as dt

_ABSENT = ("", "-")
_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _now() -> dt.datetime:
    """Host-local wall clock. Module-level so tests can patch one point."""
    return dt.datetime.now()


def parse_not_before(v: object) -> dt.datetime | None:
    """The naive local datetime a `not_before` value defers until, or None.

    Tolerates `datetime` / `date` objects too: a `yaml.safe_load` reader turns
    an unquoted `2026-10-12 17:00:00` into a datetime. A tz-aware datetime is
    converted to host-local time. A string that matches none of the three
    shapes but starts with a valid date (`2026-10-12 (after exam)`) keeps the
    date-only gate the old `[:10]` comparison gave it.
    """
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            return v.astimezone().replace(tzinfo=None)
        return v
    if isinstance(v, dt.date):
        return dt.datetime.combine(v, dt.time.min)
    s = str(v).strip().strip("\"'").strip()
    if s in _ABSENT:
        return None
    s = s.replace("T", " ", 1)
    for fmt in _FORMATS:
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.combine(dt.date.fromisoformat(s[:10]), dt.time.min)
    except ValueError:
        return None


def is_deferred(v: object, now: dt.datetime | dt.date | None = None) -> bool:
    """True while `v` names a moment still in the future.

    `now` defaults to `_now()`; a bare `date` means 00:00 that day, which keeps
    callers that only know "today" on the old date-only semantics.
    """
    p = parse_not_before(v)
    if p is None:
        return False
    if now is None:
        now = _now()
    elif not isinstance(now, dt.datetime):
        now = dt.datetime.combine(now, dt.time.min)
    return p > now


def display_not_before(v: object) -> str:
    """Wire form for views: `YYYY-MM-DD` when the gate is at 00:00 (date-only,
    or an explicit `00:00`, which means the same), else `YYYY-MM-DDTHH:MM`
    (seconds dropped). "" when there is no gate."""
    p = parse_not_before(v)
    if p is None:
        return ""
    if p.time() == dt.time.min:
        return p.date().isoformat()
    return p.strftime("%Y-%m-%dT%H:%M")
