"""Tests for `deps.write_integration_task` / `deps.git_remote_url` — the paths that
write auto-generated integration-check tasks into the vault without human approval
before the orchestrator dispatches them. Every write goes to a temp `items_dir`,
never the real vault `Todo/items/`."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from todo_system.cli import parse_frontmatter
from todo_system.macp import deps as D


def _spec(**overrides) -> dict:
    spec = {
        "slug": "consumer-integcheck-upstream",
        "title": "consumer: upstream 변경 통합 검증",
        "project": "consumer",
        "root": "",
        "worker_mode": "delegate",
        "outcome": "upstream의 최신 변경이 consumer에 호환되는지 검증",
        "reason": "upstream 'sdk-v2' 변경",
    }
    spec.update(overrides)
    return spec


class WriteIntegrationTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.items_dir = self.tmp / "items"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- AC①③④: parseable file, path, body -------------------------------

    def test_write_creates_parseable_file(self):
        spec = _spec()
        path = D.write_integration_task(spec, items_dir=self.items_dir)

        self.assertEqual(path, self.items_dir / f"{spec['slug']}.md")
        self.assertTrue(path.is_file())

        text = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        self.assertNotEqual(fm, {})
        self.assertEqual(fm["status"], "open")
        self.assertEqual(fm["worker_mode"], "delegate")
        self.assertEqual(fm["auto_merge"], "false")
        self.assertIn("macp-propagated", fm["tags"])

        self.assertIn(f"# {spec['title']}", body)
        self.assertIn(spec["reason"], body)
        self.assertIn("## Progress Log", body)

    # ---- AC②: repo: line presence follows git_remote_url -------------------

    def test_repo_line_absent_when_root_empty(self):
        spec = _spec(root="")
        path = D.write_integration_task(spec, items_dir=self.items_dir)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("repo:", text)

    def test_worker_mode_defaults_to_delegate_when_key_absent(self):
        spec = _spec()
        del spec["worker_mode"]
        path = D.write_integration_task(spec, items_dir=self.items_dir)
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        self.assertEqual(fm["worker_mode"], "delegate")
        self.assertEqual(fm["auto_merge"], "false")

    def test_repo_line_absent_when_root_key_absent(self):
        spec = _spec()
        del spec["root"]
        with patch.object(D, "git_remote_url") as mock_remote_url:
            path = D.write_integration_task(spec, items_dir=self.items_dir)
        mock_remote_url.assert_not_called()
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("repo:", text)

    def test_repo_line_present_when_git_remote_url_nonempty(self):
        spec = _spec(root=str(self.tmp / "some-repo"))
        with patch.object(D, "git_remote_url", return_value="git@example.com:x/y.git"):
            path = D.write_integration_task(spec, items_dir=self.items_dir)
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        self.assertIn("repo:", text)
        self.assertEqual(fm.get("repo"), "git@example.com:x/y.git")

    # ---- AC③: re-invocation overwrites, no duplicate files ------------------

    def test_rewrite_overwrites_no_duplicate(self):
        spec = _spec()
        D.write_integration_task(spec, items_dir=self.items_dir)
        spec2 = _spec(outcome="다른 outcome", reason="다른 reason")
        path2 = D.write_integration_task(spec2, items_dir=self.items_dir)

        files = list(self.items_dir.glob("*.md"))
        self.assertEqual(len(files), 1)
        text = path2.read_text(encoding="utf-8")
        self.assertIn("다른 reason", text)

    # ---- AC⑤: reason containing double quotes must not break frontmatter ---

    def test_reason_with_double_quote_does_not_break_frontmatter(self):
        # Confirms whether the naive f-string quoting in deps.py:154-162 breaks
        # when `reason` itself contains a `"`. If this goes RED, deps.py's quoting
        # is fixed minimally (escape only) per the task's acceptance criterion ⑤;
        # if it stays GREEN (as observed — the line-oriented parser only strips
        # the outer quote chars and never looks for a colon inside `reason`),
        # deps.py is left untouched.
        spec = _spec(reason='이슈 "critical" 발견')
        path = D.write_integration_task(spec, items_dir=self.items_dir)
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        self.assertNotEqual(fm, {})
        self.assertEqual(fm["status"], "open")
        self.assertIn("critical", fm.get("next_action", ""))

    # ---- AC⑥: git_remote_url on non-git dir / timeout returns "" ------------

    def test_git_remote_url_non_git_dir_returns_empty(self):
        non_git = self.tmp / "nogit"
        non_git.mkdir()
        self.assertEqual(D.git_remote_url(non_git), "")

    def test_git_remote_url_local_repo_with_fake_origin(self):
        repo_dir = self.tmp / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_dir), check=True,
                        capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin",
                         "git@example.com:x/fake.git"], check=True,
                        capture_output=True, text=True)
        self.assertEqual(D.git_remote_url(repo_dir), "git@example.com:x/fake.git")

    def test_git_remote_url_timeout_returns_empty(self):
        with patch.object(D.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5)):
            self.assertEqual(D.git_remote_url(self.tmp), "")


if __name__ == "__main__":
    unittest.main()
