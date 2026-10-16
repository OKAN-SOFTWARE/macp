#!/usr/bin/env python3
"""Build the KakaoTalk memo message for today's brief.

Reads the brief path + today date from env vars, prints the final message
(<= 180 chars to stay well under Kakao's ~200-char text limit) to stdout.
Lives in its own file because mixing this Python source with a heredoc
inside ``$(...)`` confuses zsh on parenthesis-heavy lines.
"""

from __future__ import annotations

import os
import re
import sys


MAX_TOTAL = 180  # safety margin below Kakao's ~200-char text limit


def main() -> int:
    brief_path = os.environ.get("BRIEF_FILE", "")
    today = os.environ.get("TODAY", "")
    if not brief_path or not today:
        print("BRIEF_FILE and TODAY env vars required", file=sys.stderr)
        return 1

    try:
        with open(brief_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        print(f"could not read brief: {exc}", file=sys.stderr)
        return 1

    # 1) Summary: first '> ...' blockquote line.
    summary = "오늘의 brief가 준비되었습니다."
    for line in text.splitlines():
        if line.startswith("> "):
            summary = line[2:].strip()
            break

    # 2) Top 3 titles only (skip 왜 우선 / 다음 액션 sub-bullets).
    in_top = False
    top_titles: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("## 🎯 Top 3"):
            in_top = True
            continue
        if in_top and raw.startswith("## "):
            break
        if not in_top:
            continue
        # Match a numbered title line like '1. **title text** ...'
        m = re.match(r"\s*(\d+)\.\s+\*\*(.+?)\*\*", raw)
        if m:
            top_titles.append(f"{m.group(1)}. {m.group(2)}")
            if len(top_titles) == 3:
                break

    # 3) Mail one-liner: the brief's 📮 section opens with '> 📮 ...'
    #    (mailroom's 21:30 kakao digest was folded into this message 2026-09-11).
    mail_line = ""
    for line in text.splitlines():
        if line.startswith("> 📮"):
            mail_line = line[2:].strip()
            break

    header = f"📋 {today} Brief"

    def assemble(titles: list[str], mail: str) -> str:
        parts = [header, "", summary]
        if mail:
            parts.append(mail)
        if titles:
            parts.append("")
            parts.extend(titles)
        return "\n".join(parts)

    msg = assemble(top_titles, mail_line)
    # Trim Top entries one at a time so the summary and mail line stay intact.
    while len(msg) > MAX_TOTAL and top_titles:
        top_titles.pop()
        msg = assemble(top_titles, mail_line)
    if len(msg) > MAX_TOTAL and mail_line:
        mail_line = ""
        msg = assemble(top_titles, mail_line)

    if len(msg) > MAX_TOTAL:
        # Header + summary alone overflowed — trim summary with ellipsis.
        keep = MAX_TOTAL - len(header) - 4
        summary = summary[: max(0, keep - 1)] + "…"
        msg = f"{header}\n\n{summary}"

    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
