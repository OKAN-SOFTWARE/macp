"""Unit tests for the stdlib-only Slack webhook backend (scripts/slack_send.py).

The script is driven as a subprocess against a throwaway vault whose
``.secrets/notify-slack.json`` points every channel at a **local** HTTP server
(``127.0.0.1:0`` — a kernel-assigned free port), so no test can reach a real
Slack webhook and the real secret is never read. The server records each
received request (path, headers, decoded JSON body) and replays a scripted
``(code, body)`` queue, which is how escaping / truncation / link grammar are
asserted on the exact payload Slack would receive.

Pure helpers (escape, link encoding, truncation) are also imported directly so
their boundaries are pinned without a round trip.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SLACK_PY = REPO_ROOT / "scripts" / "slack_send.py"

pytestmark = pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(),
    reason="slack_send.py runs under /usr/bin/python3",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("slack_send_under_test", SLACK_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


slack_send = _load_module()


class RecordingSlackServer:
    """A local webhook endpoint: records requests, replays a scripted response
    queue (the last entry repeats). Shared by every channel path."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        # Each entry: (http_code, body, extra_headers). Default: success.
        self.responses: list[tuple[int, str, dict]] = [(200, "ok", {})]
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence stderr noise
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                server.requests.append({
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "authorization": self.headers.get("Authorization"),
                    "json": body,
                })
                idx = min(len(server.requests) - 1, len(server.responses) - 1)
                code, resp_body, headers = server.responses[idx]
                payload = resp_body.encode("utf-8")
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, channel: str) -> str:
        return f"http://127.0.0.1:{self.port}/hook/{channel}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def server():
    srv = RecordingSlackServer()
    yield srv
    srv.close()


class SlackVault:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.secrets = root / ".secrets"
        self.secrets.mkdir(parents=True)

    def write_webhooks(self, mapping: dict[str, str]) -> None:
        (self.secrets / "notify-slack.json").write_text(
            json.dumps({"webhooks": mapping}, ensure_ascii=False), encoding="utf-8")

    def write_bot(
        self, token: str, channels: dict[str, str], webhooks: dict[str, str] | None = None,
    ) -> None:
        secret: dict = {"bot_token": token, "channels": channels}
        if webhooks is not None:
            secret["webhooks"] = webhooks
        (self.secrets / "notify-slack.json").write_text(
            json.dumps(secret, ensure_ascii=False), encoding="utf-8")

    def run(self, *args: str, **env_extra: str | None):
        env = {
            "PATH": os.environ["PATH"],
            "KAKAO_VAULT_DIR": str(self.root),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        for key, value in env_extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(
            ["/usr/bin/python3", str(SLACK_PY), *args],
            env=env, capture_output=True, text=True, errors="replace",
            timeout=60, check=False,
        )


@pytest.fixture
def vault(tmp_path) -> SlackVault:
    return SlackVault(tmp_path / "vault")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_escape_only_touches_amp_lt_gt():
    assert slack_send._escape_mrkdwn("a & b < c > d") == "a &amp; b &lt; c &gt; d"
    # Slack formatting characters are preserved verbatim.
    assert slack_send._escape_mrkdwn("*bold* _it_ ~s~") == "*bold* _it_ ~s~"


def test_link_url_percent_encodes_pipe_and_gt():
    out = slack_send.build_text("hi", "https://x.test/a|b>c")
    assert out == "hi <https://x.test/a%7Cb%3Ec|열기>"


def test_truncation_only_over_4000_no_180_cut():
    assert slack_send.build_text("가" * 300, None) == "가" * 300        # NOT cut to 180
    assert slack_send.build_text("나" * 4000, None) == "나" * 4000      # boundary kept
    cut = slack_send.build_text("다" * 4001, None)
    assert len(cut) == 4000 and cut == "다" * 3999 + "…"


# ---------------------------------------------------------------------------
# Channel selection
# ---------------------------------------------------------------------------


def test_profile_selects_its_own_webhook(vault, server):
    vault.write_webhooks({"default": server.url("default"), "kis": server.url("kis")})
    result = vault.run("체결", KAKAO_PROFILE="kis")

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 1
    assert server.requests[0]["path"] == "/hook/kis"
    assert server.requests[0]["json"] == {"text": "체결"}
    assert server.requests[0]["content_type"] == "application/json"


def test_notify_channel_key_wins_over_profile(vault, server):
    vault.write_webhooks({"default": server.url("default"), "digest": server.url("digest")})
    result = vault.run("daily", KAKAO_PROFILE="kis", NOTIFY_CHANNEL_KEY="digest")

    assert result.returncode == 0, result.stderr
    assert server.requests[0]["path"] == "/hook/digest"


def test_unknown_channel_falls_back_to_default(vault, server):
    vault.write_webhooks({"default": server.url("default")})
    result = vault.run("hi", KAKAO_PROFILE="nope")

    assert result.returncode == 0, result.stderr
    assert server.requests[0]["path"] == "/hook/default"


def test_no_default_and_unknown_channel_skips_with_no_http(vault, server):
    vault.write_webhooks({"kis": server.url("kis")})
    result = vault.run("hi", KAKAO_PROFILE="nope")

    assert result.returncode != 0
    assert server.requests == []          # zero HTTP calls
    assert "no webhook" in result.stderr


# ---------------------------------------------------------------------------
# Payload shaping over the wire
# ---------------------------------------------------------------------------


def test_escape_applied_to_sent_body(vault, server):
    vault.write_webhooks({"default": server.url("default")})
    assert vault.run("a & b < c > d *keep*").returncode == 0
    assert server.requests[0]["json"]["text"] == "a &amp; b &lt; c &gt; d *keep*"


def test_explicit_link_becomes_open_suffix(vault, server):
    vault.write_webhooks({"default": server.url("default")})
    assert vault.run("예매 열림", "https://example.com/x").returncode == 0
    assert server.requests[0]["json"]["text"] == "예매 열림 <https://example.com/x|열기>"


def test_no_link_arg_sends_bare_message(vault, server):
    vault.write_webhooks({"default": server.url("default")})
    assert vault.run("plain").returncode == 0
    assert server.requests[0]["json"]["text"] == "plain"


def test_over_4000_body_is_cut_not_at_180(vault, server):
    vault.write_webhooks({"default": server.url("default")})
    assert vault.run("가" * 5000).returncode == 0
    text = server.requests[0]["json"]["text"]
    assert len(text) == 4000 and text.endswith("…")


# ---------------------------------------------------------------------------
# Failure handling + secrecy
# ---------------------------------------------------------------------------


def test_http_error_is_nonzero_and_masks_url(vault, server):
    server.responses = [(404, "channel_not_found", {})]
    vault.write_webhooks({"default": server.url("default")})
    result = vault.run("hi")

    assert result.returncode == 1
    assert "http=404" in result.stderr
    assert "channel_not_found" in result.stderr
    # The webhook URL is a credential: it must not appear anywhere.
    assert server.url("default") not in (result.stdout + result.stderr)


def test_500_with_body_is_nonzero(vault, server):
    server.responses = [(500, "no_service", {})]
    vault.write_webhooks({"default": server.url("default")})
    result = vault.run("hi")

    assert result.returncode == 1
    assert "http=500" in result.stderr


def test_200_without_ok_body_is_treated_as_failure(vault, server):
    server.responses = [(200, "invalid_payload", {})]
    vault.write_webhooks({"default": server.url("default")})
    result = vault.run("hi")

    assert result.returncode == 1


def test_network_error_is_nonzero_and_leaks_no_url(vault):
    # tmp vault with a webhook pointing at a port with nothing listening.
    import tempfile
    root = Path(tempfile.mkdtemp()) / "vault"
    v = SlackVault(root)
    dead = "http://127.0.0.1:1/hook/default"
    v.write_webhooks({"default": dead})
    result = v.run("hi")

    assert result.returncode == 1
    assert dead not in (result.stdout + result.stderr)
    assert "network-error" in result.stderr


def test_429_retries_once_honouring_retry_after(vault, server):
    server.responses = [(429, "rate_limited", {"Retry-After": "0"}), (200, "ok", {})]
    vault.write_webhooks({"default": server.url("default")})
    result = vault.run("hi")

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 2      # original + one retry


def test_webhook_url_never_appears_on_success(vault, server):
    vault.write_webhooks({"default": server.url("default")})
    result = vault.run("hi")

    assert result.returncode == 0, result.stderr
    assert server.url("default") not in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# Bot-token backend (B): chat.postMessage over SLACK_API_BASE
# ---------------------------------------------------------------------------
#
# The (B) shape points at the same RecordingSlackServer, but the method URL is
# redirected with SLACK_API_BASE so the script POSTs to ``<base>/chat.postMessage``.
# Slack answers HTTP 200 even on failure, so the bot path must inspect the JSON
# ``ok`` field — these tests script that body explicitly.


def test_bot_success_posts_chat_postmessage(vault, server):
    server.responses = [(200, json.dumps({"ok": True}), {})]
    vault.write_bot("xoxb-test-token", {"default": "C_DEFAULT"})
    result = vault.run("체결", SLACK_API_BASE=f"http://127.0.0.1:{server.port}")

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 1
    req = server.requests[0]
    assert req["path"] == "/chat.postMessage"
    assert req["content_type"] == "application/json"
    assert req["authorization"] == "Bearer xoxb-test-token"
    assert req["json"]["channel"] == "C_DEFAULT"
    assert req["json"]["text"] == "체결"
    assert req["json"]["unfurl_links"] is False
    # Token and channel ID are credentials: never on stdout/stderr.
    assert "xoxb-test-token" not in (result.stdout + result.stderr)
    assert "C_DEFAULT" not in (result.stdout + result.stderr)


def test_bot_channel_selection_and_default_fallback(vault, server):
    server.responses = [(200, json.dumps({"ok": True}), {})]
    vault.write_bot("xoxb-x", {"default": "C_DEF", "digest": "C_DIG"})

    # NOTIFY_CHANNEL_KEY wins over KAKAO_PROFILE, same as the webhook path.
    picked = vault.run(
        "daily", KAKAO_PROFILE="kis", NOTIFY_CHANNEL_KEY="digest",
        SLACK_API_BASE=f"http://127.0.0.1:{server.port}",
    )
    assert picked.returncode == 0, picked.stderr
    assert server.requests[-1]["json"]["channel"] == "C_DIG"

    # Unknown key falls back to 'default'.
    fell = vault.run(
        "hi", KAKAO_PROFILE="nope",
        SLACK_API_BASE=f"http://127.0.0.1:{server.port}",
    )
    assert fell.returncode == 0, fell.stderr
    assert server.requests[-1]["json"]["channel"] == "C_DEF"


def test_bot_no_channel_skips_with_no_http(vault, server):
    server.responses = [(200, json.dumps({"ok": True}), {})]
    vault.write_bot("xoxb-x", {"kis": "C_KIS"})
    result = vault.run(
        "hi", KAKAO_PROFILE="nope",
        SLACK_API_BASE=f"http://127.0.0.1:{server.port}",
    )

    assert result.returncode != 0
    assert server.requests == []          # zero HTTP calls
    assert "no channel" in result.stderr


def test_bot_ok_false_is_failure_and_masks_ids(vault, server):
    server.responses = [(200, json.dumps({"ok": False, "error": "not_in_channel"}), {})]
    vault.write_bot("xoxb-secret", {"default": "C_SECRET"})
    result = vault.run("hi", SLACK_API_BASE=f"http://127.0.0.1:{server.port}")

    assert result.returncode == 1
    assert "ok=false" in result.stderr
    assert "error=not_in_channel" in result.stderr
    assert "http=200" in result.stderr
    # Token and channel ID must not leak into the masked failure summary.
    assert "xoxb-secret" not in (result.stdout + result.stderr)
    assert "C_SECRET" not in (result.stdout + result.stderr)


def test_bot_non_json_body_is_failure(vault, server):
    server.responses = [(200, "<html>oops</html>", {})]
    vault.write_bot("xoxb-x", {"default": "C_DEF"})
    result = vault.run("hi", SLACK_API_BASE=f"http://127.0.0.1:{server.port}")

    assert result.returncode == 1
    assert "ok=false" in result.stderr
    assert "error=non_json" in result.stderr


def test_bot_429_retries_once(vault, server):
    server.responses = [
        (429, "", {"Retry-After": "0"}),
        (200, json.dumps({"ok": True}), {}),
    ]
    vault.write_bot("xoxb-x", {"default": "C_DEF"})
    result = vault.run("hi", SLACK_API_BASE=f"http://127.0.0.1:{server.port}")

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 2      # original + one retry


def test_bot_preferred_over_webhooks(vault, server):
    server.responses = [(200, json.dumps({"ok": True}), {})]
    # Both shapes present in one secret: the bot path must win.
    vault.write_bot(
        "xoxb-x", {"default": "C_DEF"},
        webhooks={"default": server.url("default")},
    )
    result = vault.run("hi", SLACK_API_BASE=f"http://127.0.0.1:{server.port}")

    assert result.returncode == 0, result.stderr
    assert len(server.requests) == 1
    # Routed to chat.postMessage, not the webhook's /hook/default path.
    assert server.requests[0]["path"] == "/chat.postMessage"
