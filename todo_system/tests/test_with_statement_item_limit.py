"""No ``with`` statement in ``todo_system/`` may reach CPython's block limit.

CPython spends one static block per ``with`` item, and the compiler caps static
nesting at 20. A 21st item is a ``SyntaxError: too many statically nested
blocks`` raised at compile time, so pytest reports one collection error for
the file and every test in it vanishes. ruff does not model this limit, so the
only warning is the import failure itself. The mock-patch helpers in
``orchestrator/tests/test_runner.py`` grew one patch at a time to 18, 19 and
20 items before they were moved to ``contextlib.ExitStack``
(macp-test-runner-exitstack-nested-block-limit).

The guard reads source with ``ast.parse``, which accepts a 21-item ``with``
(the limit is enforced by the compiler, not the parser), so a file that no
longer imports is still named here with its line number.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # todo_system/
# At 20 items a statement already sits on the limit with no headroom left.
LIMIT = 20
# test_runner.py's patch helpers go through ExitStack; a plain `with` there
# stays small.
RUNNER_MAX = 12


def _with_item_counts(source: str, filename: str = "<fixture>") -> list[tuple[int, int]]:
    tree = ast.parse(source, filename)
    return [(node.lineno, len(node.items)) for node in ast.walk(tree)
            if isinstance(node, (ast.With, ast.AsyncWith))]


def _violations(limit: int) -> list[str]:
    hits = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, n in _with_item_counts(path.read_text(encoding="utf-8"), rel):
            if n >= limit:
                hits.append(f"{rel}:{lineno} ({n} items)")
    return hits


def _fixture(n: int) -> str:
    return "with " + ", ".join(f"cm{i}()" for i in range(n)) + ":\n    pass\n"


def test_fixture_at_limit_is_flagged():
    # RED control for the counter itself: a 20-item statement must count as a
    # violation and a 19-item one must not, or the tree check below is hollow.
    assert _with_item_counts(_fixture(20)) == [(1, 20)]
    assert _with_item_counts(_fixture(20))[0][1] >= LIMIT
    assert _with_item_counts(_fixture(19)) == [(1, 19)]
    assert _with_item_counts(_fixture(19))[0][1] < LIMIT


def test_fixture_past_compile_limit_still_parses():
    # The guard must be able to name a file that no longer compiles. The
    # compile() SyntaxError itself is not asserted: the limit is a CPython
    # implementation detail that a future version may raise.
    assert _with_item_counts(_fixture(21)) == [(1, 21)]


def test_no_with_statement_reaches_block_limit():
    hits = _violations(LIMIT)
    assert hits == [], (
        f"`with` statements at or past CPython's {LIMIT}-block limit; "
        f"use contextlib.ExitStack instead:\n" + "\n".join(hits))


def test_runner_tests_keep_with_statements_small():
    path = ROOT / "orchestrator" / "tests" / "test_runner.py"
    counts = _with_item_counts(path.read_text(encoding="utf-8"), str(path))
    worst = max(counts, key=lambda c: c[1])
    assert worst[1] <= RUNNER_MAX, (
        f"test_runner.py:{worst[0]} has a {worst[1]}-item `with`; "
        f"enter patches through contextlib.ExitStack")
