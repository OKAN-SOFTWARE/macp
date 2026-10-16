"""Characterization tests for ``scripts/_build_brief_message.py``.

Drives the script as a subprocess (it is a pure, argument-free script that
reads ``BRIEF_FILE``/``TODAY`` env vars and prints the assembled memo to
stdout) so the 180-char truncation cascade is exercised the same way the
real morning brief pipeline invokes it. Follows the ``_run``/env-dict
pattern established in ``test_kakao_send.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "_build_brief_message.py"

MAX_TOTAL = 180


def _run(
    brief_text: str | None,
    today: str | None,
    tmp_path: Path,
    **env_overrides: str | None,
) -> subprocess.CompletedProcess[str]:
    env: dict[str, str] = {
        "PATH": os.environ["PATH"],
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    # Forward the coverage subprocess seam only when a caller (the AFTER
    # measurement run) has set it up — absent, this script runs uninstrumented.
    for key in ("COVERAGE_PROCESS_START", "COVERAGE_FILE"):
        if key in os.environ:
            env[key] = os.environ[key]

    if brief_text is None:
        brief_path = tmp_path / "missing.md"
    else:
        brief_path = tmp_path / "brief.md"
        brief_path.write_text(brief_text, encoding="utf-8")

    if today is not None:
        env["TODAY"] = today
    env["BRIEF_FILE"] = str(brief_path)

    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_missing_env_vars_exits_1(tmp_path):
    result = _run("hello", "2026-09-27", tmp_path, BRIEF_FILE=None, TODAY=None)

    assert result.returncode == 1
    assert "BRIEF_FILE and TODAY env vars required" in result.stderr


def test_unreadable_brief_file_exits_1(tmp_path):
    result = _run(None, "2026-09-27", tmp_path)

    assert result.returncode == 1
    assert "could not read brief:" in result.stderr


def test_normal_assembly_includes_summary_top3_and_mail(tmp_path):
    brief = """# Daily Brief

> 오늘은 계약서 검토에 집중하세요.

## 🎯 Top 3

1. **계약서 초안 검토**
   - 왜: 마감 임박
2. **버그 수정 PR 머지**

## 📮 Mail

> 📮 미확인 메일 2건
"""
    result = _run(brief, "2026-09-27", tmp_path)

    assert result.returncode == 0, result.stderr
    stdout = result.stdout
    assert "📋 2026-09-27 Brief" in stdout
    assert "오늘은 계약서 검토에 집중하세요." in stdout
    assert "1. 계약서 초안 검토" in stdout
    assert "2. 버그 수정 PR 머지" in stdout
    assert "📮 미확인 메일 2건" in stdout
    # Relative order: header -> summary -> mail -> top titles (assemble()'s order).
    idx = {
        "header": stdout.index("📋 2026-09-27 Brief"),
        "summary": stdout.index("오늘은 계약서 검토에 집중하세요."),
        "mail": stdout.index("📮 미확인 메일 2건"),
        "title1": stdout.index("1. 계약서 초안 검토"),
        "title2": stdout.index("2. 버그 수정 PR 머지"),
    }
    assert idx["header"] < idx["summary"] < idx["mail"] < idx["title1"] < idx["title2"]


def test_summary_falls_back_when_no_blockquote_line(tmp_path):
    brief = "# Daily Brief\n\nNo blockquote line here at all.\n"
    result = _run(brief, "2026-09-27", tmp_path)

    assert result.returncode == 0, result.stderr
    assert "오늘의 brief가 준비되었습니다." in result.stdout


def test_truncation_drops_top_titles_first(tmp_path):
    long_title_1 = "가" * 70
    long_title_2 = "나" * 70
    long_title_3 = "다" * 70
    brief = f"""# Daily Brief

> 짧은 요약.

## 🎯 Top 3

1. **{long_title_1}**
2. **{long_title_2}**
3. **{long_title_3}**

## 📮 Mail

> 📮 짧은 메일.
"""
    result = _run(brief, "2026-09-27", tmp_path)

    assert result.returncode == 0, result.stderr
    stdout = result.stdout.rstrip("\n")
    assert len(stdout) <= MAX_TOTAL
    assert "짧은 메일." in stdout
    # At least the last title (dropped first, per pop()) must be gone.
    assert long_title_3 not in stdout


def test_truncation_drops_mail_line_when_titles_alone_not_enough(tmp_path):
    long_mail = "메" * 200
    brief = f"""# Daily Brief

> 짧은 요약.

## 📮 Mail

> 📮 {long_mail}
"""
    result = _run(brief, "2026-09-27", tmp_path)

    assert result.returncode == 0, result.stderr
    stdout = result.stdout.rstrip("\n")
    assert len(stdout) <= MAX_TOTAL
    assert long_mail not in stdout
    assert "짧은 요약." in stdout


def test_truncation_ellipsis_trims_summary_as_last_resort(tmp_path):
    today = "2026-09-27"
    long_summary = "요" * 250
    brief = f"# Daily Brief\n\n> {long_summary}\n"
    result = _run(brief, today, tmp_path)

    assert result.returncode == 0, result.stderr
    header = f"📋 {today} Brief"
    keep = MAX_TOTAL - len(header) - 4
    expected_summary = long_summary[: max(0, keep - 1)] + "…"
    expected = f"{header}\n\n{expected_summary}"

    stdout = result.stdout.rstrip("\n")
    assert stdout == expected
    assert len(stdout) <= MAX_TOTAL
