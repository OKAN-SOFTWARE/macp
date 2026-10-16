"""Tests for the agent registry (P1).

`RegistryTest` exercises the routing/notify/uniqueness contract against an
anonymous shipped fixture (``fixtures/registry-sample.yaml``) so it holds in the
public engine repo with no live vault present. `RealRegistryTest` is a lenient
smoke over the live ``Worker/agents/registry.yaml`` when it exists (skipped
otherwise); it asserts the shape (parses, ≥1 human, every human reachable) and
never names a personal agent id.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator.registry import KIND_HUMAN, load_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "registry-sample.yaml"


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())  # for catch-all / bad-yaml variants
        self.reg = load_registry(FIXTURE)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_and_kind(self):
        self.assertEqual(self.reg.get("primary-human").kind, KIND_HUMAN)
        self.assertTrue(self.reg.get("primary-human").is_human)
        self.assertFalse(self.reg.get("demo-worker").is_human)

    def test_glob_project_match(self):
        a = self.reg.for_project("demo-beta")
        self.assertIsNotNone(a)
        self.assertEqual(a.id, "primary-human")  # matched demo-*

    def test_exact_beats_glob(self):
        # both primary-human (demo-*) and demo-worker (exact) match demo-alpha;
        # the exact listing wins.
        a = self.reg.for_project("demo-alpha")
        self.assertEqual(a.id, "demo-worker")

    def test_unowned_project(self):
        self.assertIsNone(self.reg.for_project("some-random-slug"))

    def test_notify_channel(self):
        self.assertIn("chat", self.reg.get("primary-human").notify)

    def test_ids_unique(self):
        self.assertEqual(
            len(self.reg.agents), len({a.id for a in self.reg.agents})
        )

    def test_catch_all_is_lowest_priority(self):
        reg = load_registry(self._catch_all_yaml())
        # specific agents still win
        self.assertEqual(reg.for_project("demo-alpha").id, "demo-worker")
        self.assertEqual(reg.for_project("demo-beta").id, "primary-human")
        # anything unmatched falls to the catch-all
        self.assertEqual(reg.for_project("worldgraph-4-relation").id, "worker-default")

    def _catch_all_yaml(self):
        p = self.tmp / "catchall.yaml"
        p.write_text(FIXTURE.read_text()
                     + '  - id: worker-default\n    kind: ai-local\n'
                       '    projects: ["*"]\n')
        return p

    def test_humans(self):
        self.assertEqual({a.id for a in self.reg.humans()}, {"primary-human"})

    def test_duplicate_ids_rejected(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("agents:\n  - {id: x, kind: human}\n  - {id: x, kind: human}\n")
        with self.assertRaises(ValueError):
            load_registry(bad)


class RealRegistryTest(unittest.TestCase):
    """The shipped Worker/agents/registry.yaml must parse and be reachable.

    Lenient by design so it never couples this file to vault data: no personal
    agent id is asserted by name. When the live file is absent (public repo /
    no vault) the smoke skips.
    """

    def test_ships_valid(self):
        live = REPO_ROOT / "Worker" / "agents" / "registry.yaml"
        if not live.exists():
            self.skipTest("live Worker/agents/registry.yaml not present (public repo / no vault)")
        reg = load_registry(live)
        humans = reg.humans()
        self.assertGreaterEqual(len(humans), 1, "live registry defines no human")
        for h in humans:
            self.assertTrue(h.notify, f"human {h.id} has no notify channel")


if __name__ == "__main__":
    unittest.main()
