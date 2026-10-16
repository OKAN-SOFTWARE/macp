"""Who owns a task — the human, or the worker pipeline.

Single source of truth for the `worker_mode` → owner mapping, shared by every
surface that must not show a human work that an agent is already doing
(NOW/FOCUS, the dashboard, Google Calendar). Lives in its own module because
`cli.py` imports `calendar_sync.py`, so the mapping cannot live in either.
"""

from __future__ import annotations

# worker_mode values that mean "a human explicitly owns this despite it
# living in a worker-ish cluster" (e.g. `manual` for API-key/login steps a
# worker can't perform). Anything else non-empty (auto, delegate, blocked,
# review, ...) is the worker pipeline's responsibility — Worker/queue.json +
# Dashboard "🤖 Worker Agent 현황" — and must never be surfaced as something a
# human needs to act on themselves.
HUMAN_OVERRIDE_WORKER_MODES = frozenset({"", "-", "off", "manual"})


def is_worker_owned(worker_mode: str | None) -> bool:
    normalized = (worker_mode or "").strip().lower()
    return normalized not in HUMAN_OVERRIDE_WORKER_MODES


def human_owned_sql_filter(column: str = "worker_mode") -> tuple[str, list[str]]:
    """SQL predicate + params selecting only human-owned tasks.

    `TRIM` mirrors `is_worker_owned`'s `.strip()`: without it a padded value
    (`'  '`, `'manual '`) read as human-owned by the Python path but
    worker-owned here, so a task a human owns was dropped from the calendar
    while still showing up in NOW/FOCUS. Parity is pinned against real sqlite
    by `OwnershipTest.test_sql_filter_agrees_with_is_worker_owned`.
    """
    placeholders = ", ".join("?" for _ in HUMAN_OVERRIDE_WORKER_MODES)
    return (
        f"LOWER(TRIM(COALESCE({column}, ''))) IN ({placeholders})",
        list(HUMAN_OVERRIDE_WORKER_MODES),
    )
