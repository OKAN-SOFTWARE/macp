"""``.gitignore`` must ignore coverage artifacts.

Without this, ``pytest --cov`` (or any coverage run) leaves ``.coverage`` /
``htmlcov/`` / ``coverage.xml`` untracked-but-dirty in the vault root — and a
dirty tree outside ``Worker/``·``Memory/episodes/`` stalls the queue worker
(same failure mode ``*.log.[0-9]`` was added to fix).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

COVERAGE_PATHS = [".coverage", ".coverage.foo", "htmlcov/index.html", "coverage.xml"]


def _is_git_ignored(relpath: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", relpath],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


def test_coverage_artifacts_are_git_ignored():
    not_ignored = [p for p in COVERAGE_PATHS if not _is_git_ignored(p)]
    assert not_ignored == [], f"not git-ignored: {not_ignored}"


def test_coverage_artifacts_not_tracked():
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = result.stdout.splitlines()
    hits = [
        f
        for f in tracked
        if f == ".coverage"
        or f.startswith(".coverage.")
        or f.startswith("htmlcov/")
        or f == "coverage.xml"
    ]
    assert hits == [], f"already tracked, would be silently newly-ignored: {hits}"


def test_export_gitignore_keeps_checkout_runtime_scratch_out():
    """A checkout install writes beta locks / sweep state / gradle registry under
    <engine>/Worker/_state (beta_publish._SCRATCH_ROOT); the exported repo's
    .gitignore must keep them out of git, as the vault's does (2026-10-16 cutover)."""
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / "scripts" / "export_engine_tree.sh"
    body = script.read_text(encoding="utf-8")
    start = body.index("GITIGNORE_EOF")
    end = body.index("GITIGNORE_EOF", start + 1)
    assert "\nWorker/_state/\n" in body[start:end]
