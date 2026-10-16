"""End-to-end tests for the Kakao shell scripts (send / refresh / oauth helper).

These drive the real ``scripts/kakao_*.sh`` files as subprocesses against a
throwaway vault, with a fake ``curl`` on ``PATH``, so **no test can reach the
real Kakao API** and no real credential under ``.secrets/`` is read or written.

Two seams make that possible:

* ``KAKAO_VAULT_DIR`` repoints ``$VAULT_DIR`` — and therefore ``.secrets/`` and
  ``logs/`` — at ``tmp_path``. Unset, every script resolves the real vault, so
  production behaviour (LaunchAgent, ``notify_kakao.sh``, ``worker.py``) is
  untouched by the seam's existence.
* the fake ``curl`` records its argv as JSON lines and prints a canned body.
  Assertions read the recorded argv, which is how the ``template_object``
  payload is inspected without an HTTP round trip.

The subprocess environment is built from scratch rather than inherited, so a
developer who happens to export ``KAKAO_REST_API_KEY`` cannot leak the real
default-app credential into a test — which matters most for
``test_named_profile_ignores_ambient_env_credentials``.

The scripts are ``#!/bin/zsh`` but are invoked here with ``bash``: CI runs on
``ubuntu-latest``, which has no zsh, and ``kakao_send.sh`` already shells out to
the refresh script with ``bash``. Both shells have to keep working, so stick to
syntax valid in each (no ``mapfile``: this repo's macOS bash is still 3.2).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SEND_SH = SCRIPTS_DIR / "kakao_send.sh"
REFRESH_SH = SCRIPTS_DIR / "kakao_token_refresh.sh"
OAUTH_SH = SCRIPTS_DIR / "kakao_oauth_helper.sh"

# The default card link is the vault's own repo, resolved from the throwaway
# vault's .macp/config.toml (KakaoVault writes TEST_REPO_SLUG there) — so the
# goldens below pin the resolver path, not a literal owner baked into the script.
TEST_REPO_SLUG = "acme/MaestroACP"
DEFAULT_LINK = f"https://github.com/{TEST_REPO_SLUG}"

# Every script hardcodes /usr/bin/python3 for its JSON work, and so does the
# fake curl below.
pytestmark = pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(),
    reason="kakao scripts hardcode /usr/bin/python3",
)


FAKE_CURL = r'''#!/usr/bin/python3
"""Stand-in for curl: records argv, prints a canned response, never hits network."""
import json
import os
import sys

argv = sys.argv[1:]
log_path = os.environ["KAKAO_TEST_CURL_LOG"]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv, ensure_ascii=False) + "\n")


def field(name):
    for i, arg in enumerate(argv):
        if arg == "--data-urlencode" and i + 1 < len(argv):
            key, _, value = argv[i + 1].partition("=")
            if key == name:
                return value
    return ""


url = next((a for a in argv if a.startswith("http")), "")

if "kauth.kakao.com/oauth/token" in url:
    broken = [c for c in os.environ.get("KAKAO_TEST_BROKEN_CLIENTS", "").split(",") if c]
    if field("client_id") in broken:
        print(json.dumps({"error": "invalid_grant", "error_description": "test"}))
    else:
        print(json.dumps({"access_token": "NEW-" + field("client_id"), "expires_in": 21599}))
    sys.exit(0)

# Memo send. KAKAO_TEST_MEMO_CODES is consumed in order; the last entry repeats.
codes = [c for c in os.environ.get("KAKAO_TEST_MEMO_CODES", "200").split(",") if c]
sent = 0
with open(log_path, encoding="utf-8") as fh:
    for line in fh:
        if "talk/memo/default/send" in line:
            sent += 1
print("{}")
print("HTTP_CODE:" + codes[min(sent - 1, len(codes) - 1)], end="")
'''


def _urlencoded_field(argv: list[str], name: str) -> str:
    """Read a ``--data-urlencode name=value`` pair out of a recorded argv."""
    for i, arg in enumerate(argv):
        if arg == "--data-urlencode" and i + 1 < len(argv):
            key, _, value = argv[i + 1].partition("=")
            if key == name:
                return value
    return ""


class KakaoVault:
    """A throwaway vault the kakao scripts are pointed at via ``KAKAO_VAULT_DIR``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.secrets = root / ".secrets"
        self.bin = root / "bin"
        self.curl_log = root / "curl_argv.jsonl"
        self.secrets.mkdir(parents=True)
        self.bin.mkdir(parents=True)
        self.curl_log.touch()
        (root / ".macp").mkdir()
        (root / ".macp" / "config.toml").write_text(
            f'repo_slug = "{TEST_REPO_SLUG}"\n', encoding="utf-8")
        # kakao_send.sh self-heals by running "$VAULT_DIR/scripts/kakao_token_refresh.sh".
        # Symlinking keeps that path live against the scripts under test.
        (root / "scripts").symlink_to(SCRIPTS_DIR)
        curl = self.bin / "curl"
        curl.write_text(FAKE_CURL, encoding="utf-8")
        curl.chmod(0o755)

    def token_path(self, profile: str = "default") -> Path:
        name = "kakao-token.json" if profile == "default" else f"kakao-token-{profile}.json"
        return self.secrets / name

    def app_path(self, profile: str = "default") -> Path:
        name = "kakao-app.json" if profile == "default" else f"kakao-app-{profile}.json"
        return self.secrets / name

    def write_token(self, profile: str = "default", **extra) -> Path:
        payload = {"access_token": f"AT-{profile}", "refresh_token": f"RT-{profile}"}
        payload.update(extra)
        path = self.token_path(profile)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def write_app(
        self,
        profile: str = "default",
        rest_api_key: str | None = None,
        client_secret: str | None = None,
        display_name: str | None = None,
    ) -> Path:
        payload: dict[str, str] = {"rest_api_key": rest_api_key or f"KEY-{profile}"}
        if client_secret is not None:
            payload["client_secret"] = client_secret
        if display_name is not None:
            payload["display_name"] = display_name
        path = self.app_path(profile)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def read_token(self, profile: str = "default") -> dict:
        return json.loads(self.token_path(profile).read_text(encoding="utf-8"))

    def write_slack_webhooks(self, mapping: dict[str, str]) -> Path:
        """Create .secrets/notify-slack.json so the dispatcher selects the slack
        backend (the default rule: kakao + slack when the secret exists)."""
        path = self.secrets / "notify-slack.json"
        path.write_text(json.dumps({"webhooks": mapping}, ensure_ascii=False),
                        encoding="utf-8")
        return path

    def calls(self) -> list[list[str]]:
        text = self.curl_log.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def memo_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if any("talk/memo/default/send" in a for a in c)]

    def refresh_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if any("kauth.kakao.com/oauth/token" in a for a in c)]

    def sent_bearers(self) -> list[str]:
        out = []
        for call in self.memo_calls():
            for i, arg in enumerate(call):
                if arg == "-H" and i + 1 < len(call) and call[i + 1].startswith("Authorization:"):
                    out.append(call[i + 1].split("Bearer ", 1)[-1])
        return out

    def sent_template_raw(self, index: int = 0) -> str:
        """The exact ``template_object`` JSON string handed to curl."""
        return _urlencoded_field(self.memo_calls()[index], "template_object")

    def sent_template(self, index: int = 0) -> dict:
        return json.loads(self.sent_template_raw(index))

    def refresh_client_ids(self) -> list[str]:
        return [_urlencoded_field(c, "client_id") for c in self.refresh_calls()]

    def env(self, **extra: str | None) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "KAKAO_VAULT_DIR": str(self.root),
            "KAKAO_TEST_CURL_LOG": str(self.curl_log),
            # Deterministic "…" handling regardless of the host locale.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, script: Path, *args: str, shell: str = "bash", **env_extra: str | None):
        # errors="replace": the scripts' own log lines use bash/zsh substring
        # slicing (`${MESSAGE:0:80}`), which cuts by *bytes* under the C locale
        # this minimal env produces and can split a UTF-8 sequence. That is a
        # pre-existing cosmetic quirk of the log line only — the payload is
        # built by python3 and is asserted separately — so decode leniently
        # instead of letting it crash the harness.
        return subprocess.run(
            [shell, str(script), *args],
            env=self.env(**env_extra),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )

    def send(self, *args: str, shell: str = "bash", **env_extra: str | None):
        return self.run(SEND_SH, *args, shell=shell, **env_extra)

    def refresh(self, *args: str, shell: str = "bash", **env_extra: str | None):
        return self.run(REFRESH_SH, *args, shell=shell, **env_extra)


@pytest.fixture
def vault(tmp_path) -> KakaoVault:
    return KakaoVault(tmp_path / "vault")


# ---------------------------------------------------------------------------
# Slack backend fanout: a local 127.0.0.1 webhook endpoint stands in for Slack,
# so the dispatcher's HTTP leg is exercised without leaving the box. Kakao still
# flows through the fake curl above; the two backends are independent.
# ---------------------------------------------------------------------------


class SlackRecorder:
    """Local webhook: records POST bodies, replays a scripted response queue."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: list[tuple[int, str, dict]] = [(200, "ok", {})]
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                server.requests.append({"path": self.path, "json": body})
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

    def texts(self) -> list[str]:
        return [r["json"]["text"] for r in self.requests if r["json"]]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def slack():
    srv = SlackRecorder()
    yield srv
    srv.close()


# ---------------------------------------------------------------------------
# Characterization: KAKAO_PROFILE unset must stay byte-identical to the
# pre-profile script. These goldens were pinned against the unmodified
# kakao_send.sh before profile support existed — treat a diff here as a
# regression in the default notification path, not as a golden to update.
# ---------------------------------------------------------------------------


def test_default_golden_template_is_byte_identical(vault):
    vault.write_token()
    result = vault.send("hello")

    assert result.returncode == 0, result.stderr
    expected = {
        "object_type": "text",
        "text": "hello",
        "link": {"web_url": DEFAULT_LINK, "mobile_web_url": DEFAULT_LINK},
        "button_title": "열기",
    }
    # Compare the raw string, not just the parsed dict: this pins key order and
    # separators, i.e. the actual bytes that reach Kakao.
    assert vault.sent_template_raw() == json.dumps(expected, ensure_ascii=False)
    assert vault.sent_bearers() == ["AT-default"]


def test_default_link_env_repo_slug_wins_over_config(vault):
    vault.write_token()
    assert vault.send("hello", MACP_REPO_SLUG="other-org/Vault").returncode == 0
    assert vault.sent_template()["link"]["web_url"] == "https://github.com/other-org/Vault"


def test_default_link_without_config_is_the_neutral_placeholder(vault):
    """No env and no repo_slug key -> the resolver's neutral default, never a
    personal owner baked into the script."""
    (vault.root / ".macp" / "config.toml").write_text("profile = 'x'\n", encoding="utf-8")
    vault.write_token()
    assert vault.send("hello").returncode == 0
    assert vault.sent_template()["link"]["web_url"] == "https://github.com/example-org/MaestroACP"


def test_default_golden_honours_explicit_link(vault):
    vault.write_token()
    result = vault.send("hello", "https://example.com/x")

    assert result.returncode == 0, result.stderr
    template = vault.sent_template()
    assert template["link"] == {
        "web_url": "https://example.com/x",
        "mobile_web_url": "https://example.com/x",
    }


def test_default_truncates_at_179_plus_ellipsis(vault):
    """Kakao silently drops over-long memo text — the 180자 guard must not regress."""
    vault.write_token()
    message = "가" * 300
    result = vault.send(message)

    assert result.returncode == 0, result.stderr
    text = vault.sent_template()["text"]
    assert len(text) == 180
    assert text == "가" * 179 + "…"


def test_default_at_the_180_char_boundary_is_not_truncated(vault):
    vault.write_token()
    message = "나" * 180
    result = vault.send(message)

    assert result.returncode == 0, result.stderr
    assert vault.sent_template()["text"] == message


def test_over_long_message_logs_a_truncation_warning(vault):
    """The last-resort cut still happens (golden above) but is no longer silent:
    it appends `WARN truncated <원래길이>→180` to logs/kakao_send.out.log so a cut
    caused by a caller that failed to self-limit is at least visible."""
    vault.write_token()
    result = vault.send("가" * 300)

    assert result.returncode == 0, result.stderr
    assert vault.sent_template()["text"] == "가" * 179 + "…"   # the cut itself is unchanged
    # errors="replace": the pre-existing `sent (200): ${MESSAGE:0:80}` log line
    # slices by bytes under the C locale and can split a UTF-8 sequence (a known
    # cosmetic quirk, see KakaoVault.run) — the WARN line we add is valid UTF-8.
    log = (vault.root / "logs" / "kakao_send.out.log").read_text(
        encoding="utf-8", errors="replace")
    assert "WARN truncated 300→180" in log


def test_boundary_message_logs_no_truncation_warning(vault):
    """At exactly 180 chars nothing is cut, so no WARN line is written."""
    vault.write_token()
    vault.send("나" * 180)

    log_path = vault.root / "logs" / "kakao_send.out.log"
    log = (log_path.read_text(encoding="utf-8", errors="replace")
           if log_path.exists() else "")
    assert "WARN truncated" not in log


def test_missing_default_token_skips_quietly(vault):
    """No token at all: warn, send nothing, still exit 0 (callers must not break)."""
    result = vault.send("hello")

    assert result.returncode == 0
    assert "token file missing" in result.stderr
    assert vault.memo_calls() == []


def test_missing_default_token_is_fatal_under_strict(vault):
    result = vault.send("hello", KAKAO_SEND_STRICT="1")

    assert result.returncode == 1
    assert vault.memo_calls() == []


def test_explicit_default_profile_still_uses_the_text_template(vault):
    """`KAKAO_PROFILE=default` is a synonym for unset, not a fourth profile."""
    vault.write_token()
    result = vault.send("hello", KAKAO_PROFILE="default")

    assert result.returncode == 0, result.stderr
    expected = {
        "object_type": "text",
        "text": "hello",
        "link": {"web_url": DEFAULT_LINK, "mobile_web_url": DEFAULT_LINK},
        "button_title": "열기",
    }
    assert vault.sent_template_raw() == json.dumps(expected, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Profile selection, feed template, fallback
# ---------------------------------------------------------------------------


def test_profile_selects_files(vault):
    """KAKAO_PROFILE=<name> reads kakao-token-<name>.json, not the default token."""
    vault.write_token()
    vault.write_token("cgv")
    vault.write_app("cgv", display_name="CGV 아이맥스")

    result = vault.send("예매 열림", KAKAO_PROFILE="cgv")

    assert result.returncode == 0, result.stderr
    # The bearer proves which token file was read.
    assert vault.sent_bearers() == ["AT-cgv"]


def test_profile_feed_template(vault):
    vault.write_token("cgv")
    vault.write_app("cgv", display_name="CGV 아이맥스")

    result = vault.send("예매 열림", "https://example.com/cgv", KAKAO_PROFILE="cgv")

    assert result.returncode == 0, result.stderr
    template = vault.sent_template()
    link = {"web_url": "https://example.com/cgv", "mobile_web_url": "https://example.com/cgv"}
    assert template == {
        "object_type": "feed",
        "content": {"title": "CGV 아이맥스", "description": "예매 열림", "link": link},
        "item_content": {"profile_text": "CGV 아이맥스"},
        "buttons": [{"title": "열기", "link": link}],
    }


def test_profile_display_name_falls_back_to_profile_name(vault):
    """App file present but without display_name → label with the profile name."""
    vault.write_token("kis")
    vault.write_app("kis")

    result = vault.send("체결", KAKAO_PROFILE="kis")

    assert result.returncode == 0, result.stderr
    template = vault.sent_template()
    assert template["item_content"]["profile_text"] == "kis"
    assert template["content"]["title"] == "kis"


def test_profile_without_app_file_still_sends_as_the_profile(vault):
    """A missing *app* file only costs the label — the token is still the profile's."""
    vault.write_token("tennis")

    result = vault.send("코트 예약", KAKAO_PROFILE="tennis")

    assert result.returncode == 0, result.stderr
    assert vault.sent_bearers() == ["AT-tennis"]
    assert vault.sent_template()["item_content"]["profile_text"] == "tennis"


def test_missing_profile_falls_back_to_default(vault):
    """Missing profile *token* → warn on stderr, send on the default identity, rc 0."""
    vault.write_token()
    vault.write_app("cgv", display_name="CGV 아이맥스")  # app exists, token does not

    result = vault.send("예매 열림", KAKAO_PROFILE="cgv")

    assert result.returncode == 0, result.stderr
    assert "falling back to default" in result.stderr
    assert vault.sent_bearers() == ["AT-default"]
    template = vault.sent_template()
    # Full fallback: no feed carrying a foreign profile_text on the default token.
    assert template["object_type"] == "text"
    assert "CGV" not in vault.sent_template_raw()


def test_missing_profile_and_missing_default_token_skips_quietly(vault):
    result = vault.send("hello", KAKAO_PROFILE="cgv")

    assert result.returncode == 0
    assert "falling back to default" in result.stderr
    assert vault.memo_calls() == []


@pytest.mark.parametrize("bad", ["../kakao-token", "cgv/../../etc/passwd", "a b", "cgv;rm"])
def test_invalid_profile_name_is_refused(vault, bad):
    """A profile name is pasted into a .secrets/ path — path traversal must not resolve."""
    vault.write_token()
    result = vault.send("hello", KAKAO_PROFILE=bad)

    assert result.returncode == 0, result.stderr
    assert "invalid KAKAO_PROFILE" in result.stderr
    assert vault.sent_bearers() == ["AT-default"]
    assert vault.sent_template()["object_type"] == "text"


def test_profile_feed_description_truncates_like_text(vault):
    """The feed description limit is unverified, so it keeps the text guard's 179+…."""
    vault.write_token("cgv")
    vault.write_app("cgv", display_name="CGV")

    result = vault.send("가" * 300, KAKAO_PROFILE="cgv")

    assert result.returncode == 0, result.stderr
    description = vault.sent_template()["content"]["description"]
    assert description == "가" * 179 + "…"


# ---------------------------------------------------------------------------
# Production-shell parity
#
# The scripts are #!/bin/zsh and the LaunchAgent really does run them under zsh,
# but the suite drives them with bash so it can run on CI's zsh-less ubuntu.
# That gap is where a shell-specific parse bug hides: while writing the feed
# template, an apostrophe in a comment *inside* the <<'PY' heredoc broke the
# whole script, because bash scans the enclosing $( ) for its closing paren
# straight through the heredoc body. These cases re-run the core paths under
# zsh whenever zsh exists, so a construct that only one shell accepts is caught
# locally instead of in production.
# ---------------------------------------------------------------------------

SHELLS = ["bash"] + (["zsh"] if shutil.which("zsh") else [])


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_parity_default_payload(vault, shell):
    vault.write_token()
    result = vault.send("hello", shell=shell)

    assert result.returncode == 0, result.stderr
    expected = {
        "object_type": "text",
        "text": "hello",
        "link": {"web_url": DEFAULT_LINK, "mobile_web_url": DEFAULT_LINK},
        "button_title": "열기",
    }
    assert vault.sent_template_raw() == json.dumps(expected, ensure_ascii=False)


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_parity_profile_feed(vault, shell):
    vault.write_token("cgv")
    vault.write_app("cgv", display_name="CGV 아이맥스")

    result = vault.send("예매 열림", shell=shell, KAKAO_PROFILE="cgv")

    assert result.returncode == 0, result.stderr
    assert vault.sent_bearers() == ["AT-cgv"]
    assert vault.sent_template()["item_content"]["profile_text"] == "CGV 아이맥스"


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_parity_profile_fallback(vault, shell):
    vault.write_token()

    result = vault.send("hello", shell=shell, KAKAO_PROFILE="cgv")

    assert result.returncode == 0, result.stderr
    assert "falling back to default" in result.stderr
    assert vault.sent_bearers() == ["AT-default"]


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_parity_invalid_profile(vault, shell):
    vault.write_token()

    result = vault.send("hello", shell=shell, KAKAO_PROFILE="../evil")

    assert result.returncode == 0, result.stderr
    assert "invalid KAKAO_PROFILE" in result.stderr
    assert vault.sent_bearers() == ["AT-default"]


# ---------------------------------------------------------------------------
# Noise-tier routing (KAKAO_LEVEL / KAKAO_EVENT_KEY opt-in → kakao_route.py)
#
# The env unset is the backward-compat contract: every golden above runs with
# neither var, so those tests double as the "unrouted callers are byte-
# identical" proof. These exercise the opt-in block itself. State files land
# under the throwaway vault (KAKAO_VAULT_DIR flows into the shim), so no test
# touches logs/ in the real vault. Window arithmetic is unit-tested with an
# injected clock in test_kakao_routing.py; here two back-to-back sends are
# trivially inside the real-time window.
# ---------------------------------------------------------------------------


def test_route_env_unset_creates_no_routing_state(vault):
    vault.write_token()
    assert vault.send("hello").returncode == 0
    logs = vault.root / "logs"
    assert not (logs / "kakao_digest.jsonl").exists()
    assert not (logs / "kakao_dedup.json").exists()
    assert not (logs / "kakao_route.log").exists()


def test_route_digest_queues_and_sends_nothing(vault):
    vault.write_token()
    result = vault.send("🤖 Worker 시작: t — FYI", KAKAO_LEVEL="digest")

    assert result.returncode == 0, result.stderr
    assert vault.memo_calls() == []                       # zero individual sends
    queue = (vault.root / "logs" / "kakao_digest.jsonl").read_text(encoding="utf-8")
    assert "Worker 시작" in queue                          # ...but nothing was lost
    assert "route: QUEUED" in result.stderr


def test_route_digest_is_recorded_even_without_a_token(vault):
    """Routing runs before token checks: a token outage must not drop a digest."""
    result = vault.send("fyi", KAKAO_LEVEL="digest")

    assert result.returncode == 0
    assert "fyi" in (vault.root / "logs" / "kakao_digest.jsonl").read_text(encoding="utf-8")


def test_route_silent_sends_nothing_but_logs(vault):
    vault.write_token()
    result = vault.send("기대된 동작", KAKAO_LEVEL="silent")

    assert result.returncode == 0, result.stderr
    assert vault.memo_calls() == []
    assert "SILENT" in (vault.root / "logs" / "kakao_route.log").read_text(encoding="utf-8")
    assert "route: SILENT" in result.stderr


def test_route_immediate_env_still_sends_normally(vault):
    vault.write_token()
    result = vault.send("hello", KAKAO_LEVEL="immediate")

    assert result.returncode == 0, result.stderr
    assert vault.sent_template()["text"] == "hello"


def test_route_event_key_merges_a_back_to_back_duplicate(vault):
    vault.write_token()
    assert vault.send("같은 사건", KAKAO_EVENT_KEY="login:win").returncode == 0
    assert vault.send("같은 사건", KAKAO_EVENT_KEY="login:win").returncode == 0

    assert len(vault.memo_calls()) == 1                   # merged into one delivery
    log = (vault.root / "logs" / "kakao_route.log").read_text(encoding="utf-8")
    assert "SUPPRESSED" in log


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_parity_route_digest(vault, shell):
    vault.write_token()
    result = vault.send("fyi", shell=shell, KAKAO_LEVEL="digest")

    assert result.returncode == 0, result.stderr
    assert vault.memo_calls() == []
    assert (vault.root / "logs" / "kakao_digest.jsonl").exists()


# ---------------------------------------------------------------------------
# kakao_token_refresh.sh — per-profile loop and exit-code contract
# ---------------------------------------------------------------------------


def _all_profiles(vault: KakaoVault) -> None:
    for profile in ("default", "cgv", "kis", "tennis"):
        vault.write_token(profile)
        vault.write_app(profile, client_secret=f"SECRET-{profile}")


def test_refresh_loops_every_profile_with_its_own_app_credentials(vault):
    _all_profiles(vault)

    result = vault.refresh()

    assert result.returncode == 0, result.stderr
    assert sorted(vault.refresh_client_ids()) == [
        "KEY-cgv",
        "KEY-default",
        "KEY-kis",
        "KEY-tennis",
    ]
    # Each token file received the access_token minted for its own client_id.
    for profile in ("default", "cgv", "kis", "tennis"):
        assert vault.read_token(profile)["access_token"] == f"NEW-KEY-{profile}"


def test_refresh_preserves_refresh_token_when_response_omits_it(vault):
    """Kakao does not return refresh_token every time — a clobber would lock us out."""
    vault.write_token("cgv")
    vault.write_app("cgv")

    result = vault.refresh("cgv")

    assert result.returncode == 0, result.stderr
    assert vault.read_token("cgv")["refresh_token"] == "RT-cgv"


def test_named_profile_ignores_ambient_env_credentials(vault):
    """The LaunchAgent exports the *default* app's key — it must not reach cgv/kis/tennis.

    Refreshing a named profile with the default app's client_id makes Kakao
    reject the call, so the token would quietly stop rotating and expire within
    hours. This is the single highest-risk regression in the change.
    """
    _all_profiles(vault)

    result = vault.refresh(
        KAKAO_REST_API_KEY="ENV-DEFAULT-KEY",
        KAKAO_CLIENT_SECRET="ENV-DEFAULT-SECRET",
    )

    assert result.returncode == 0, result.stderr
    client_ids = vault.refresh_client_ids()
    # The default profile still honours the env (today's contract, unchanged)...
    assert "ENV-DEFAULT-KEY" in client_ids
    assert client_ids.count("ENV-DEFAULT-KEY") == 1
    # ...and every named profile used its own app file instead.
    assert sorted(c for c in client_ids if c != "ENV-DEFAULT-KEY") == [
        "KEY-cgv",
        "KEY-kis",
        "KEY-tennis",
    ]


def test_default_profile_env_still_takes_precedence_over_app_file(vault):
    vault.write_token()
    vault.write_app(rest_api_key="FILE-KEY")

    result = vault.refresh("default", KAKAO_REST_API_KEY="ENV-KEY")

    assert result.returncode == 0, result.stderr
    assert vault.refresh_client_ids() == ["ENV-KEY"]


def test_one_broken_profile_does_not_abort_the_others(vault):
    """set -e must not let a single dead profile strand the ones queued behind it."""
    _all_profiles(vault)

    result = vault.refresh(KAKAO_TEST_BROKEN_CLIENTS="KEY-kis")

    assert result.returncode == 0, result.stderr
    assert len(vault.refresh_client_ids()) == 4
    for profile in ("default", "cgv", "tennis"):
        assert vault.read_token(profile)["access_token"] == f"NEW-KEY-{profile}"
    # The broken one kept its old token rather than being half-written.
    assert vault.read_token("kis")["access_token"] == "AT-kis"
    assert "3 refreshed, 1 failed" in result.stdout


def test_missing_app_file_skips_that_profile_without_falling_back_to_env(vault):
    _all_profiles(vault)
    vault.app_path("tennis").unlink()

    result = vault.refresh(KAKAO_REST_API_KEY="ENV-DEFAULT-KEY")

    assert result.returncode == 0, result.stderr
    assert "KEY-tennis" not in vault.refresh_client_ids()
    # Crucially it was skipped, not retried on the default app's credential.
    assert vault.refresh_client_ids().count("ENV-DEFAULT-KEY") == 1
    assert vault.read_token("tennis")["access_token"] == "AT-tennis"


def test_all_profiles_failing_exits_nonzero(vault):
    _all_profiles(vault)

    result = vault.refresh(
        KAKAO_TEST_BROKEN_CLIENTS="KEY-default,KEY-cgv,KEY-kis,KEY-tennis"
    )

    assert result.returncode == 1
    assert "0 refreshed, 4 failed" in result.stdout


def test_no_token_files_at_all_exits_nonzero(vault):
    result = vault.refresh()

    assert result.returncode == 1
    assert "no kakao-token*.json found" in result.stdout


def test_single_profile_mode_touches_exactly_one_profile(vault):
    _all_profiles(vault)

    result = vault.refresh("cgv")

    assert result.returncode == 0, result.stderr
    assert vault.refresh_client_ids() == ["KEY-cgv"]
    assert vault.read_token("cgv")["access_token"] == "NEW-KEY-cgv"
    for untouched in ("default", "kis", "tennis"):
        assert vault.read_token(untouched)["access_token"] == f"AT-{untouched}"


def test_single_profile_mode_reports_that_profiles_own_failure(vault):
    """kakao_send.sh's 401 retry depends on rc 0 meaning *this* token was renewed."""
    _all_profiles(vault)

    result = vault.refresh("cgv", KAKAO_TEST_BROKEN_CLIENTS="KEY-cgv")

    assert result.returncode == 1


def test_profile_can_be_passed_by_env_instead_of_argv(vault):
    _all_profiles(vault)

    result = vault.refresh(KAKAO_PROFILE="kis")

    assert result.returncode == 0, result.stderr
    assert vault.refresh_client_ids() == ["KEY-kis"]


# ---------------------------------------------------------------------------
# 401 self-heal: the profile must survive the hop into the refresh script
# ---------------------------------------------------------------------------


def test_401_refreshes_the_profiles_own_token_and_retries(vault):
    vault.write_token()
    vault.write_app(rest_api_key="KEY-default")
    vault.write_token("cgv")
    vault.write_app("cgv", display_name="CGV 아이맥스")

    result = vault.send("예매 열림", KAKAO_PROFILE="cgv", KAKAO_TEST_MEMO_CODES="401,200")

    assert result.returncode == 0, result.stderr
    # The refresh hop used cgv's credential, not the default app's.
    assert vault.refresh_client_ids() == ["KEY-cgv"]
    # ...and the retry went out on the freshly minted cgv token.
    assert vault.sent_bearers() == ["AT-cgv", "NEW-KEY-cgv"]
    assert vault.read_token()["access_token"] == "AT-default"


def test_401_on_default_profile_refreshes_only_default(vault):
    """Regression guard: the self-heal must not fan out to every profile."""
    _all_profiles(vault)

    result = vault.send("hello", KAKAO_TEST_MEMO_CODES="401,200")

    assert result.returncode == 0, result.stderr
    assert vault.refresh_client_ids() == ["KEY-default"]
    assert vault.sent_bearers() == ["AT-default", "NEW-KEY-default"]


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_parity_refresh_loop(vault, shell):
    """The refresh loop is what the 4-hourly LaunchAgent runs, under zsh.

    Discovery (`find` into an array), the `set -e` suspension that keeps one
    dead profile from stranding the rest, and `${#arr[@]}` all behave subtly
    differently between the two shells, so the loop is exercised in both.
    """
    _all_profiles(vault)

    result = vault.refresh(shell=shell)

    assert result.returncode == 0, result.stderr
    assert sorted(vault.refresh_client_ids()) == [
        "KEY-cgv",
        "KEY-default",
        "KEY-kis",
        "KEY-tennis",
    ]


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_parity_refresh_survives_empty_secrets_dir(vault, shell):
    """An unmatched glob aborts a zsh script outright — discovery must use find."""
    result = vault.refresh(shell=shell)

    assert result.returncode == 1
    assert "no kakao-token*.json found" in result.stdout


# ---------------------------------------------------------------------------
# kakao_oauth_helper.sh — onboarding a new app as a profile
#
# The real path opens a browser and waits 120s for a callback, so
# KAKAO_OAUTH_DRY_RUN is the only seam these can drive. Path resolution is
# what matters: get it wrong and the token lands where refresh will not look.
# ---------------------------------------------------------------------------


def _dry_run(vault: KakaoVault, *args: str, **env_extra: str | None) -> dict[str, str]:
    result = vault.run(
        OAUTH_SH,
        *args,
        KAKAO_OAUTH_DRY_RUN="1",
        KAKAO_REST_API_KEY="TEST-KEY",
        **env_extra,
    )
    assert result.returncode == 0, result.stderr
    fields = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


def test_oauth_helper_no_argument_keeps_the_default_filenames(vault):
    fields = _dry_run(vault)

    assert fields["profile"] == "default"
    assert fields["token_file"] == str(vault.secrets / "kakao-token.json")
    assert fields["app_file"] == str(vault.secrets / "kakao-app.json")


def test_oauth_helper_profile_argument_suffixes_both_files(vault):
    fields = _dry_run(vault, "cgv")

    assert fields["profile"] == "cgv"
    assert fields["token_file"] == str(vault.secrets / "kakao-token-cgv.json")
    assert fields["app_file"] == str(vault.secrets / "kakao-app-cgv.json")


def test_oauth_helper_accepts_the_profile_from_env(vault):
    fields = _dry_run(vault, KAKAO_PROFILE="kis")

    assert fields["token_file"] == str(vault.secrets / "kakao-token-kis.json")


def test_oauth_helper_display_name_defaults_to_the_profile(vault):
    assert _dry_run(vault, "tennis")["display_name"] == "tennis"
    assert _dry_run(vault, "tennis", KAKAO_DISPLAY_NAME="테니스")["display_name"] == "테니스"


def test_oauth_helper_unset_vault_dir_resolves_to_its_own_checkout(vault):
    """KAKAO_VAULT_DIR unset -> the parent of the directory the script is run
    from (was a hardcoded macOS home path that pointed nowhere on the Linux HQ).
    Run through the throwaway vault's ``scripts`` symlink so the resolved vault is
    ``vault.root``, never the real checkout."""
    result = vault.run(
        vault.root / "scripts" / "kakao_oauth_helper.sh",
        KAKAO_OAUTH_DRY_RUN="1",
        KAKAO_REST_API_KEY="TEST-KEY",
        KAKAO_VAULT_DIR=None,
    )
    assert result.returncode == 0, result.stderr
    assert f"token_file: {vault.secrets / 'kakao-token.json'}" in result.stdout.splitlines()


def test_oauth_helper_rejects_a_traversing_profile_name(vault):
    result = vault.run(
        OAUTH_SH,
        "../evil",
        KAKAO_OAUTH_DRY_RUN="1",
        KAKAO_REST_API_KEY="TEST-KEY",
    )

    assert result.returncode == 1
    assert "invalid profile name" in result.stderr


def test_oauth_helper_paths_match_what_the_other_scripts_read(vault):
    """The three scripts must agree on filenames, or onboarding silently no-ops."""
    fields = _dry_run(vault, "cgv")

    vault.write_token("cgv")
    vault.write_app("cgv", display_name="CGV")
    assert fields["token_file"] == str(vault.token_path("cgv"))
    assert fields["app_file"] == str(vault.app_path("cgv"))

    # ...and those are the files kakao_send.sh and the refresh loop actually use.
    assert vault.send("hi", KAKAO_PROFILE="cgv").returncode == 0
    assert vault.sent_bearers() == ["AT-cgv"]
    assert vault.refresh("cgv").returncode == 0
    assert vault.refresh_client_ids() == ["KEY-cgv"]


# ---------------------------------------------------------------------------
# Backend dispatcher + Slack fanout (kakao_send.sh as a channel-agnostic sender)
#
# The routed $MESSAGE is fanned out to every selected backend; routing itself
# runs once, before the fanout. These pin the AC set AFTER the Slack-only
# cutover: the DEFAULT (unset NOTIFY_BACKENDS) is Slack-alone when the secret is
# present (zero Kakao HTTP) and Kakao-alone when it is absent (fallback, zero
# Slack HTTP); NOTIFY_BACKENDS=kakao rolls back to Kakao and =kakao,slack fans
# out to both. Plus: channel mapping, escaping / link grammar, routing-once,
# non-fatal Slack failure, STRICT-primary, URL masking, and the Slack-only
# length rule (no 180자 cut). Tests that still need to observe the Kakao request
# pin NOTIFY_BACKENDS="kakao,slack" explicitly, since the default no longer fires
# Kakao when a Slack secret exists.
# ---------------------------------------------------------------------------


def test_backcompat_no_secret_no_backends_fires_zero_slack(vault, slack):
    """AC (Slack secret absent → Kakao fallback): no notify-slack.json and no
    NOTIFY_BACKENDS → kakao only, Slack never hit, and the Kakao request/exit stay
    exactly as the goldens above. A host without Slack never loses an alert."""
    vault.write_token()
    result = vault.send("hello")

    assert result.returncode == 0, result.stderr
    assert len(vault.memo_calls()) == 1           # kakao fired once
    assert slack.requests == []                    # zero Slack HTTP


def test_secret_present_defaults_to_slack_only(vault, slack):
    """AC (cutover): Slack secret present + NOTIFY_BACKENDS unset → Slack ALONE.
    Kakao HTTP must be zero (Slack is the live channel now)."""
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("즉시 알림")

    assert result.returncode == 0, result.stderr
    assert vault.memo_calls() == []                # zero Kakao HTTP (cutover)
    assert len(slack.requests) == 1
    assert slack.requests[0]["path"] == "/hook/default"
    assert slack.texts() == ["즉시 알림"]


def test_rollback_notify_backends_kakao_re_enables_kakao(vault, slack):
    """AC (rollback path): Slack secret present but NOTIFY_BACKENDS=kakao → Kakao
    ALONE, Slack never hit. The env-only rollback restores the pre-cutover send."""
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("롤백 알림", NOTIFY_BACKENDS="kakao")

    assert result.returncode == 0, result.stderr
    assert len(vault.memo_calls()) == 1            # kakao fired once
    assert slack.requests == []                     # zero Slack HTTP


def test_explicit_kakao_slack_fans_out_to_both_backends(vault, slack):
    """NOTIFY_BACKENDS=kakao,slack keeps the fan-out both-backends path testable
    now that the default is Slack-only."""
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("즉시 알림", NOTIFY_BACKENDS="kakao,slack")

    assert result.returncode == 0, result.stderr
    assert len(vault.memo_calls()) == 1
    assert len(slack.requests) == 1
    assert slack.requests[0]["path"] == "/hook/default"
    assert slack.texts() == ["즉시 알림"]


def test_profile_kis_routes_to_kis_webhook(vault, slack):
    vault.write_token("kis")
    vault.write_app("kis", display_name="KIS")
    vault.write_slack_webhooks({"default": slack.url("default"), "kis": slack.url("kis")})
    result = vault.send("체결", KAKAO_PROFILE="kis", NOTIFY_BACKENDS="kakao,slack")

    assert result.returncode == 0, result.stderr
    assert vault.sent_bearers() == ["AT-kis"]       # kakao used the kis token
    assert slack.requests[0]["path"] == "/hook/kis"  # slack used the kis webhook


def test_profile_absent_from_map_falls_back_to_default_webhook(vault, slack):
    vault.write_token("kis")
    vault.write_slack_webhooks({"default": slack.url("default")})   # no 'kis' key
    result = vault.send("체결", KAKAO_PROFILE="kis")

    assert result.returncode == 0, result.stderr
    assert slack.requests[0]["path"] == "/hook/default"


def test_no_default_webhook_skips_slack_with_warn(vault, slack):
    """Slack secret FILE present but its 'default' channel is unwired. Post-cutover
    the default is Slack-only, so PRIMARY=slack is skipped (WARN) and — because the
    secret exists — there is NO Kakao fallback either. This is a known alert-LOSS
    gap: a half-configured Slack secret (file present, channel missing) drops the
    alert. Non-STRICT so exit stays 0. (A host with NO secret file still falls back
    to Kakao — see test_backcompat_no_secret_no_backends_fires_zero_slack.)"""
    vault.write_token()
    vault.write_slack_webhooks({"kis": slack.url("kis")})   # no 'default', channel=default
    result = vault.send("hello")

    assert result.returncode == 0, result.stderr   # non-STRICT: primary slack fail → exit 0
    assert vault.memo_calls() == []                 # kakao not in the default backend list
    assert slack.requests == []                     # no webhook → zero HTTP
    assert "WARN slack" in result.stderr


def test_explicit_link_becomes_slack_open_suffix(vault, slack):
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("예매 열림", "https://example.com/x")

    assert result.returncode == 0, result.stderr
    assert slack.texts() == ["예매 열림 <https://example.com/x|열기>"]


def test_slack_escapes_body_and_percent_encodes_link(vault, slack):
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("a & b < c > d", "https://x.test/a|b>c")

    assert result.returncode == 0, result.stderr
    assert slack.texts() == ["a &amp; b &lt; c &gt; d <https://x.test/a%7Cb%3Ec|열기>"]


def test_default_card_link_is_not_forwarded_to_slack(vault, slack):
    """No explicit 2nd arg → Kakao still gets its default repo card link, but
    Slack must NOT inherit it (link is explicit-only)."""
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("plain", NOTIFY_BACKENDS="kakao,slack")

    assert result.returncode == 0, result.stderr
    assert slack.texts() == ["plain"]               # no <...|열기> suffix
    assert vault.sent_template()["link"]["web_url"] == DEFAULT_LINK  # kakao unchanged


def test_digest_routing_calls_no_backend(vault, slack):
    """A digest verdict exits before the fanout, so neither backend fires even
    though the Slack secret is present."""
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default"),
                                "digest": slack.url("digest")})
    result = vault.send("🤖 FYI", KAKAO_LEVEL="digest")

    assert result.returncode == 0, result.stderr
    assert vault.memo_calls() == []
    assert slack.requests == []


def test_suppressed_duplicate_calls_no_backend(vault, slack):
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    assert vault.send("같은 사건", KAKAO_EVENT_KEY="login:win",
                      NOTIFY_BACKENDS="kakao,slack").returncode == 0
    before = len(slack.requests)
    assert vault.send("같은 사건", KAKAO_EVENT_KEY="login:win",
                      NOTIFY_BACKENDS="kakao,slack").returncode == 0

    # First send fanned out once; the merged duplicate fires neither backend.
    assert len(vault.memo_calls()) == 1
    assert len(slack.requests) == before == 1


def test_slack_failure_is_non_fatal_with_kakao_primary(vault, slack):
    slack.responses = [(500, "no_service", {})]
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("hello", NOTIFY_BACKENDS="kakao,slack")

    assert result.returncode == 0           # kakao (primary) unaffected
    assert len(vault.memo_calls()) == 1
    assert "WARN slack" in result.stderr
    assert "http=500" in result.stderr
    assert "no_service" in result.stderr


def test_strict_with_kakao_primary_ignores_slack_failure(vault, slack):
    """primary=kakao (NOTIFY_BACKENDS=kakao,slack): even under STRICT a Slack
    failure does not flip exit."""
    slack.responses = [(404, "channel_not_found", {})]
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("hello", KAKAO_SEND_STRICT="1", NOTIFY_BACKENDS="kakao,slack")

    assert result.returncode == 0           # kakao succeeded → STRICT satisfied
    assert len(vault.memo_calls()) == 1


def test_strict_with_slack_primary_makes_slack_failure_fatal(vault, slack):
    """NOTIFY_BACKENDS=slack,kakao makes slack the primary — its failure under
    STRICT must exit 1, while Kakao still delivers."""
    slack.responses = [(500, "no_service", {})]
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("hello", KAKAO_SEND_STRICT="1", NOTIFY_BACKENDS="slack,kakao")

    assert result.returncode == 1
    assert len(vault.memo_calls()) == 1      # kakao still sent (non-primary, non-fatal path)


def test_webhook_url_never_appears_in_logs_or_stderr(vault, slack):
    slack.responses = [(500, "no_service", {})]
    vault.write_token()
    url = slack.url("default")
    vault.write_slack_webhooks({"default": url})
    result = vault.send("hello")

    assert result.returncode == 0, result.stderr
    log = (vault.root / "logs" / "kakao_send.out.log").read_text(
        encoding="utf-8", errors="replace")
    assert url not in result.stdout
    assert url not in result.stderr
    assert url not in log


def test_slack_has_no_180_cut_only_4000(vault, slack):
    """Slack must not inherit the Kakao 180자 budget: a 300-char body goes out
    whole, while Kakao still truncates to 180 in its own template. Pins both
    backends (NOTIFY_BACKENDS=kakao,slack) so the Kakao-side last-resort cut — a
    separate net from the raised compose budget — is still exercised post-cutover."""
    vault.write_token()
    vault.write_slack_webhooks({"default": slack.url("default")})
    result = vault.send("가" * 300, NOTIFY_BACKENDS="kakao,slack")

    assert result.returncode == 0, result.stderr
    assert len(slack.texts()[0]) == 300                       # Slack: full
    assert vault.sent_template()["text"] == "가" * 179 + "…"   # Kakao: still 180
