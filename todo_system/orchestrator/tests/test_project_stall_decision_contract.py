"""Contract test: the decision-task template `render_decision_task` writes
must pass `task_validate` with ERROR 0 + WARN 0, and carry every field
`cli audit` treats as required (outcome/next_action/if_then/estimate_p50/
estimate_p90) non-empty. Companion to test_discovery.py's generator contract
— the two autogenerators (discovery, project_stall) share this bar so a
generator's own noise never drowns a human-authored task's real warning.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import yaml

from todo_system import task_validate
from todo_system.orchestrator import project_stall as ps


class RenderDecisionTaskContractTest(unittest.TestCase):
    def test_written_task_passes_validate_and_audit_contract(self):
        view = ps.ProjectView(
            slug="demo-project",
            title="Demo Project",
            tags=("todo", "work"),
            outcome="demo project outcome",
        )
        verdict = ps.StallVerdict(
            stalled=True,
            reason="all leaves done, project left open",
            done_leaves=[ps.LeafView(slug="demo-leaf-1", status="done", title="Leaf 1")],
            open_leaves=[],
        )
        text = ps.render_decision_task(view, verdict, today=dt.date(2026, 10, 15))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo-project-next-direction.md"
            path.write_text(text, encoding="utf-8")

            issues = task_validate.validate_file(path)
            errors = [i for i in issues if i.level == task_validate.ERROR]
            self.assertEqual(errors, [], [i.format("x") for i in issues])
            warns = [i for i in issues if i.level == task_validate.WARN]
            self.assertEqual(warns, [], [i.format("x") for i in issues])

            fm = yaml.safe_load(text.split("---")[1])
            for field in ("outcome", "next_action", "if_then", "estimate_p50", "estimate_p90"):
                self.assertTrue(fm.get(field), f"audit requires {field} to be non-empty")


if __name__ == "__main__":
    unittest.main()
