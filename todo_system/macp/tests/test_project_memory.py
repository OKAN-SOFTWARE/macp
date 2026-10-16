"""Contract tests for `todo_system/macp/memory.py`.

Covers `link_agent_context` idempotency + boundary cases, `_refresh_index`/
`write_memory` sort/self-exclusion and hand-written annotation/prose preservation, and
`read_memory`/`ensure_memory` boundary contracts that `test_macp.py`'s single
happy-path test does not exercise.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system.macp import memory as MEM


class ProjectMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proj"
        self.proj.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- link_agent_context ------------------------------------------------

    def test_link_agent_context_idempotent(self):
        (self.proj / "CLAUDE.md").write_text("# proj\n", encoding="utf-8")
        MEM.link_agent_context(self.proj)
        second = MEM.link_agent_context(self.proj)
        self.assertEqual(second, [])
        claude_text = (self.proj / "CLAUDE.md").read_text(encoding="utf-8")
        agents_text = (self.proj / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(claude_text.count(MEM.MEMORY_IMPORT), 1)
        self.assertEqual(agents_text.count(MEM.POINTER), 1)

    def test_link_agent_context_no_claude_md(self):
        actions = MEM.link_agent_context(self.proj)
        self.assertEqual(actions, ["AGENTS.md created"])
        self.assertFalse((self.proj / "CLAUDE.md").exists())
        self.assertTrue((self.proj / "AGENTS.md").exists())

    def test_link_agent_context_agents_md_pointer_only(self):
        (self.proj / "AGENTS.md").write_text("# existing guide\n", encoding="utf-8")
        actions = MEM.link_agent_context(self.proj)
        self.assertEqual(actions, ["AGENTS.md pointer"])
        text = (self.proj / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# existing guide\n"))
        self.assertIn(".macp/memory", text)

    def test_link_agent_context_no_trailing_newline(self):
        (self.proj / "CLAUDE.md").write_text("# proj", encoding="utf-8")
        MEM.link_agent_context(self.proj)
        text = (self.proj / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# proj\n"))
        self.assertIn(MEM.MEMORY_IMPORT, text)

    def test_link_agent_context_creates_memory_dir(self):
        MEM.link_agent_context(self.proj)
        self.assertTrue(MEM.index_path(self.proj).exists())

    def test_discover_context_empty_root(self):
        ctx = MEM.discover_context(self.proj)
        self.assertIsNone(ctx["memory_dir"])
        self.assertIsNone(ctx["claude_md"])
        self.assertIsNone(ctx["agents_md"])
        self.assertIsNone(ctx["claude_dir"])
        self.assertEqual(ctx["memory_files"], [])

    # -- _refresh_index / write_memory --------------------------------------

    def test_write_memory_refreshes_index_sorted_and_self_excluded(self):
        MEM.write_memory(self.proj, "zzz", "z")
        MEM.write_memory(self.proj, "aaa", "a")
        text = MEM.index_path(self.proj).read_text(encoding="utf-8")
        self.assertLess(text.index("aaa.md"), text.index("zzz.md"))
        self.assertNotIn("index.md.md", text)
        self.assertNotIn("(index.md)", text)

    def test_refresh_index_empty_has_no_items_section(self):
        MEM.ensure_memory(self.proj)
        text = MEM.index_path(self.proj).read_text(encoding="utf-8")
        self.assertNotIn("## 항목", text)

    def _annotate(self, old: str, new: str) -> None:
        idx = MEM.index_path(self.proj)
        text = idx.read_text(encoding="utf-8")
        self.assertIn(old, text)
        idx.write_text(text.replace(old, new), encoding="utf-8")

    def test_write_memory_preserves_handwritten_item_annotation(self):
        """A hand-written tail on an item line (`- [x.md](x.md) — 설명`) survives
        the next `write_memory()` call; the new file is listed alongside it."""
        MEM.write_memory(self.proj, "note", "x")
        self._annotate("- [note.md](note.md)\n", "- [note.md](note.md) — 손으로 단 주석\n")
        MEM.write_memory(self.proj, "note2", "y")
        text = MEM.index_path(self.proj).read_text(encoding="utf-8")
        self.assertIn("- [note.md](note.md) — 손으로 단 주석\n", text)
        self.assertIn("- [note2.md](note2.md)\n", text)

    def test_refresh_index_drops_line_for_removed_file(self):
        MEM.write_memory(self.proj, "note", "x")
        MEM.write_memory(self.proj, "gone", "g")
        self._annotate("- [gone.md](gone.md)\n", "- [gone.md](gone.md) — 곧 지울 파일\n")
        (MEM.memory_dir(self.proj) / "gone.md").unlink()
        MEM.write_memory(self.proj, "other", "o")
        text = MEM.index_path(self.proj).read_text(encoding="utf-8")
        self.assertNotIn("gone.md", text)
        self.assertNotIn("곧 지울 파일", text)
        self.assertIn("- [note.md](note.md)\n", text)
        self.assertIn("- [other.md](other.md)\n", text)

    def test_write_memory_adds_new_file_bare_and_keeps_existing_annotation(self):
        MEM.write_memory(self.proj, "a", "a")
        MEM.write_memory(self.proj, "c", "c")
        self._annotate("- [a.md](a.md)\n", "- [a.md](a.md) — 설명 `코드` → **강조**\n")
        # The tail is kept verbatim whatever its separator — not only ` — `.
        self._annotate("- [c.md](c.md)\n", "- [c.md](c.md) (괄호 설명)\n")
        MEM.write_memory(self.proj, "b", "b")
        text = MEM.index_path(self.proj).read_text(encoding="utf-8")
        self.assertIn("- [a.md](a.md) — 설명 `코드` → **강조**\n", text)
        self.assertIn("- [b.md](b.md)\n", text)
        self.assertIn("- [c.md](c.md) (괄호 설명)\n", text)
        self.assertLess(text.index("[a.md]"), text.index("[b.md]"))
        self.assertLess(text.index("[b.md]"), text.index("[c.md]"))

    def test_write_memory_preserves_prose_outside_items_section(self):
        MEM.write_memory(self.proj, "note", "x")
        MEM.index_path(self.proj).write_text(
            "# 커스텀 머리말\n\n사람이 쓴 설명 문단.\n\n## 항목\n- [note.md](note.md)\n\n"
            "## 메모\n섹션 뒤 산문.\n", encoding="utf-8")
        MEM.write_memory(self.proj, "note2", "y")
        text = MEM.index_path(self.proj).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 커스텀 머리말\n\n사람이 쓴 설명 문단.\n"))
        self.assertIn("## 메모\n섹션 뒤 산문.\n", text)
        self.assertLess(text.index("note2.md"), text.index("## 메모"))

    def test_refresh_index_keeps_continuation_line_with_item(self):
        """A non-link line under an item travels with that item."""
        MEM.write_memory(self.proj, "note", "x")
        idx = MEM.index_path(self.proj)
        idx.write_text(idx.read_text(encoding="utf-8") + "- 손으로 단 주석\n", encoding="utf-8")
        MEM.write_memory(self.proj, "aaa", "a")
        text = idx.read_text(encoding="utf-8")
        self.assertIn("- [note.md](note.md)\n- 손으로 단 주석\n", text)
        self.assertLess(text.index("aaa.md"), text.index("note.md"))

    def test_refresh_index_is_idempotent(self):
        MEM.write_memory(self.proj, "a", "a")
        MEM.write_memory(self.proj, "b", "b")
        idx = MEM.index_path(self.proj)
        idx.write_text(
            "# 머리말\n\n산문.\n\n\n## 항목\n\n- [b.md](b.md) — 비\n  들여쓴 연속줄\n"
            "- [a.md](a.md) (에이)\n\n## 메모\n뒤 산문.\n\n", encoding="utf-8")
        MEM._refresh_index(self.proj)
        once = idx.read_text(encoding="utf-8")
        MEM._refresh_index(self.proj)
        self.assertEqual(idx.read_text(encoding="utf-8"), once)
        self.assertIn("- [a.md](a.md) (에이)\n- [b.md](b.md) — 비\n  들여쓴 연속줄\n", once)

    def test_write_memory_overwrites_same_name(self):
        MEM.write_memory(self.proj, "note", "v1")
        p = MEM.write_memory(self.proj, "note", "v2")
        self.assertEqual(p.read_text(encoding="utf-8"), "v2\n")

    # -- read_memory / ensure_memory -----------------------------------------

    def test_read_memory_skips_index_and_headers_files(self):
        MEM.write_memory(self.proj, "a", "content-a")
        MEM.write_memory(self.proj, "b", "content-b")
        result = MEM.read_memory(self.proj)
        self.assertNotIn("### index.md", result)
        self.assertIn("### a.md", result)
        self.assertIn("### b.md", result)

    def test_read_memory_limit_truncates_exactly(self):
        MEM.write_memory(self.proj, "a", "x" * 100)
        result = MEM.read_memory(self.proj, limit=10)
        self.assertEqual(len(result), 10)

    def test_read_memory_missing_dir_returns_empty_string(self):
        self.assertEqual(MEM.read_memory(self.proj), "")

    def test_ensure_memory_does_not_overwrite_existing_index(self):
        MEM.ensure_memory(self.proj)
        MEM.index_path(self.proj).write_text("custom content\n", encoding="utf-8")
        MEM.ensure_memory(self.proj)
        self.assertEqual(MEM.index_path(self.proj).read_text(encoding="utf-8"), "custom content\n")


if __name__ == "__main__":
    unittest.main()
