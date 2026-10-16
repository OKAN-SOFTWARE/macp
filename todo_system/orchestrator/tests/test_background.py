"""Tests for background-agent dispatch/parse (P3 RC integration). No real
`claude --bg` is spawned; subprocess-touching functions are exercised via
their pure parts and a monkeypatched session list."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from todo_system.orchestrator import background as bg


class BuildArgvTest(unittest.TestCase):
    def test_positional_prompt_no_print(self):
        argv = bg.build_dispatch_argv("do the thing", worktree="agent/t1",
                                      permission_mode="acceptEdits")
        self.assertEqual(argv[:3], ["claude", "--bg", "do the thing"])  # positional
        self.assertNotIn("-p", argv)
        self.assertNotIn("--print", argv)
        self.assertIn("--worktree", argv)
        self.assertIn("agent/t1", argv)
        self.assertIn("--permission-mode", argv)

    def test_minimal(self):
        self.assertEqual(bg.build_dispatch_argv("hi"), ["claude", "--bg", "hi"])


class ParseSessionIdTest(unittest.TestCase):
    def test_parses_backgrounded_line(self):
        out = ("Starting background service…\n"
               "backgrounded · c8bad7c5\n"
               "  claude agents             list sessions\n")
        self.assertEqual(bg.parse_session_id(out), "c8bad7c5")

    def test_colon_variant(self):
        self.assertEqual(bg.parse_session_id("backgrounded: ABCD1234ef"), "ABCD1234ef")

    def test_none_when_absent(self):
        self.assertIsNone(bg.parse_session_id("no id here"))


class SessionActiveTest(unittest.TestCase):
    def test_prefix_match(self):
        sessions = [
            {"sessionId": "c8bad7c5-94bf-4fe1-aeae-67a00f865ca0", "kind": "background"},
            {"sessionId": "749ab1bb-f9ba-4a19", "kind": "interactive"},
        ]
        orig = bg.list_sessions
        bg.list_sessions = lambda include_completed=False: sessions
        try:
            self.assertTrue(bg.session_active("c8bad7c5"))     # short prefix matches
            self.assertFalse(bg.session_active("deadbeef"))
        finally:
            bg.list_sessions = orig


class ListSessionsFailureTest(unittest.TestCase):
    """subprocess.run 실패/비정상 응답 3경로가 모두 [] 로 조기 반환됨을 고정."""

    def test_filenotfounderror_returns_empty(self):
        def _raise_fnf(*args, **kwargs):
            raise FileNotFoundError()

        orig = bg.subprocess.run
        bg.subprocess.run = _raise_fnf
        try:
            self.assertEqual(bg.list_sessions(), [])
        finally:
            bg.subprocess.run = orig

    def test_subprocess_error_returns_empty(self):
        def _raise_subprocess_error(*args, **kwargs):
            raise subprocess.SubprocessError()

        orig = bg.subprocess.run
        bg.subprocess.run = _raise_subprocess_error
        try:
            self.assertEqual(bg.list_sessions(), [])
        finally:
            bg.subprocess.run = orig

    def test_nonzero_returncode_returns_empty(self):
        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

        orig = bg.subprocess.run
        bg.subprocess.run = _fake_run
        try:
            self.assertEqual(bg.list_sessions(), [])
        finally:
            bg.subprocess.run = orig

    def test_non_json_stdout_returns_empty(self):
        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="not json", stderr="")

        orig = bg.subprocess.run
        bg.subprocess.run = _fake_run
        try:
            self.assertEqual(bg.list_sessions(), [])
        finally:
            bg.subprocess.run = orig


class ListSessionsSuccessTest(unittest.TestCase):
    """정상 JSON 배열 반환과 include_completed에 따른 --all 분기를 고정."""

    def test_returns_parsed_json_list(self):
        sessions = [{"sessionId": "x"}]

        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0,
                                               stdout=json.dumps(sessions), stderr="")

        orig = bg.subprocess.run
        bg.subprocess.run = _fake_run
        try:
            self.assertEqual(bg.list_sessions(), sessions)
        finally:
            bg.subprocess.run = orig

    def test_include_completed_adds_all_flag(self):
        captured = []

        def _fake_run(*args, **kwargs):
            captured.append(args[0])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")

        orig = bg.subprocess.run
        bg.subprocess.run = _fake_run
        try:
            bg.list_sessions(include_completed=True)
            self.assertIn("--all", captured[0])
            bg.list_sessions(include_completed=False)
            self.assertNotIn("--all", captured[1])
        finally:
            bg.subprocess.run = orig


class SessionActiveEmptyTest(unittest.TestCase):
    def test_empty_session_list_is_inactive(self):
        """빈 세션 목록(예: claude CLI 미가용)일 때 session_active 가 False —
        runner.py:209 의 종료 오판 방지 경로."""
        orig = bg.list_sessions
        bg.list_sessions = lambda include_completed=False: []
        try:
            self.assertFalse(bg.session_active("anything"))
        finally:
            bg.list_sessions = orig


class DispatchTest(unittest.TestCase):
    """dispatch 가 subprocess.run 에 넘기는 argv/cwd/timeout/capture_output 계약과
    backgrounded 파싱 성공/실패 두 경로를 고정."""

    def test_backgrounded_line_returns_session_id_and_asserts_call_contract(self):
        captured_kwargs = {}

        def _fake_run(*args, **kwargs):
            captured_kwargs["args"] = args
            captured_kwargs.update(kwargs)
            return subprocess.CompletedProcess(args=args, returncode=0,
                                               stdout="backgrounded · c8bad7c5", stderr="")

        orig = bg.subprocess.run
        bg.subprocess.run = _fake_run
        try:
            result = bg.dispatch("prompt", cwd=Path("/tmp/x"), worktree="agent/t1",
                                 permission_mode="acceptEdits")
        finally:
            bg.subprocess.run = orig

        session_id, proc = result
        self.assertEqual(session_id, "c8bad7c5")
        self.assertEqual(proc.stdout, "backgrounded · c8bad7c5")
        self.assertEqual(captured_kwargs["args"][0],
                         bg.build_dispatch_argv("prompt", "agent/t1", "acceptEdits"))
        self.assertIsInstance(captured_kwargs["cwd"], str)
        self.assertEqual(captured_kwargs["cwd"], "/tmp/x")
        self.assertEqual(captured_kwargs["timeout"], 120)
        self.assertIs(captured_kwargs["capture_output"], True)

    def test_no_backgrounded_line_returns_none_and_proc(self):
        def _fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        orig = bg.subprocess.run
        bg.subprocess.run = _fake_run
        try:
            session_id, proc = bg.dispatch("prompt", cwd="/tmp/x")
        finally:
            bg.subprocess.run = orig

        self.assertIsNone(session_id)
        self.assertEqual(proc.stdout, "")


class WatchPointerTest(unittest.TestCase):
    def test_pointers(self):
        w = bg.watch_pointer("c8bad7c5")
        self.assertEqual(w["attach"], "claude attach c8bad7c5")
        self.assertEqual(w["logs"], "claude logs c8bad7c5")
        self.assertEqual(w["stop"], "claude stop c8bad7c5")
        self.assertTrue(w["watch_url"].startswith("https://claude.ai/code"))


if __name__ == "__main__":
    unittest.main()
