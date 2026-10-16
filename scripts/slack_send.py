#!/usr/bin/env python3
"""Slack incoming-webhook backend for the notification dispatcher (kakao_send.sh).

A stdlib-only (urllib) sibling of the Kakao send path, so it runs under bare
``/usr/bin/python3`` from launchd / systemd just like kakao_digest_send.py.
kakao_send.sh fans the *already-routed* message out to this backend when the
caller opts in (``NOTIFY_BACKENDS``) or a ``.secrets/notify-slack.json`` secret
exists; it is also called directly by notify_kakao.sh (morning brief).

Usage:  scripts/slack_send.py "<message text>" [link_url]

Channel selection, in order: ``NOTIFY_CHANNEL_KEY`` -> ``KAKAO_PROFILE`` ->
``default``. The resolved key picks a webhook from the secret's ``webhooks`` map;
a key absent from the map falls back to ``default``. No ``default`` webhook
either -> warn (masked) and exit non-zero with zero HTTP calls.

Secret schema (``<vault>/.secrets/notify-slack.json``, 600, gitignored) — two
shapes are accepted, and the bot shape wins when both keys are present::

    (A) {"webhooks": {"default": "<url>", "kis": "<url>", "digest": "<url>"}}
    (B) {"bot_token": "xoxb-…", "channels": {"default": "<id>", "kis": "<id>"}}

(A) posts to the incoming-webhook URL as today. (B) posts to
``chat.postMessage`` with an ``Authorization: Bearer`` header and a
``{channel, text, unfurl_links}`` JSON body; because Slack answers HTTP 200 even
on failure, the response's ``ok`` field decides success. Channel-key resolution
(``NOTIFY_CHANNEL_KEY`` -> ``KAKAO_PROFILE`` -> ``default``) and the ``default``
fallback are identical for both shapes. The method base URL can be redirected
with the ``SLACK_API_BASE`` env var for local-fixture tests.

Message handling:
  * mrkdwn-safe escape of ``& < >`` only (``*_~`` preserved as formatting);
  * an *explicit* link_url is appended as `` <url|열기>`` with the URL's ``|`` and
    ``>`` percent-encoded so they cannot break the link grammar;
  * cut only when the body exceeds 4000 code points (``…``); NO 180자 cut — the
    Kakao-only budget must not shrink Slack.

Transport: ``POST`` with ``Content-Type: application/json`` and body
``{"text": ...}``. Success = HTTP 200 **and** response body ``ok`` (Slack's
incoming-webhook contract). One retry on HTTP 429, honouring ``Retry-After``
(capped at 10s).

Secrecy: the webhook URL and the bot token are both credentials, and the target
channel ID is sensitive, so none is ever written to stdout, stderr, logs or
exception text — a webhook failure is summarised as
``profile=<key> http=<code> body=<...>`` and a bot failure as
``profile=<key> http=<code> ok=false error=<slack_error>``, with the credential
and channel ID omitted. Exit 0 on a delivered message, non-zero on any failure
(the dispatcher treats a Slack failure as non-fatal for a non-primary backend).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The body cut: Slack's section text tops out near 40,000 chars, but a single
# memo that long is almost certainly a bug upstream, so the practical cut is
# 4000. Unlike the Kakao path there is no 180자 budget here.
SLACK_TEXT_LIMIT = 4000
RETRY_AFTER_CAP_SECONDS = 10


def _vault_root() -> Path:
    """Same anchor order as the kakao scripts: KAKAO_VAULT_DIR (historical test
    seam) -> MACP_VAULT_ROOT (split install) -> this checkout (combined layout)."""
    env = os.environ.get("KAKAO_VAULT_DIR") or os.environ.get("MACP_VAULT_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1]


def _channel_key() -> str:
    """NOTIFY_CHANNEL_KEY wins (digest sender sets it), then KAKAO_PROFILE, then
    the shared 'default' channel."""
    key = os.environ.get("NOTIFY_CHANNEL_KEY") or os.environ.get("KAKAO_PROFILE") or "default"
    key = key.strip()
    return key or "default"


def _load_secret(vault: Path) -> dict:
    """Read the whole secret file once. A missing/unreadable/non-object file
    yields ``{}`` so both the webhook (A) and bot (B) loaders degrade quietly."""
    secret = vault / ".secrets" / "notify-slack.json"
    try:
        data = json.loads(secret.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_webhooks(vault: Path) -> dict:
    hooks = _load_secret(vault).get("webhooks")
    return hooks if isinstance(hooks, dict) else {}


def _load_bot(secret: dict) -> tuple[str | None, dict]:
    """Return (bot_token, channels) for the (B) shape, or (None, {}) when the
    secret has no usable ``bot_token``."""
    token = secret.get("bot_token")
    if not isinstance(token, str) or not token:
        return None, {}
    channels = secret.get("channels")
    return token, channels if isinstance(channels, dict) else {}


def _select_target(mapping: dict, key: str) -> tuple[str | None, str]:
    """Return (value, resolved_key). A key missing from the map falls back to
    'default'; a missing 'default' yields (None, key). Shared by both shapes so
    channel-key resolution stays identical."""
    if key in mapping and mapping[key]:
        return mapping[key], key
    if mapping.get("default"):
        return mapping["default"], "default"
    return None, key


def _select_webhook(webhooks: dict, key: str) -> tuple[str | None, str]:
    return _select_target(webhooks, key)


def _select_channel(channels: dict, key: str) -> tuple[str | None, str]:
    return _select_target(channels, key)


def _escape_mrkdwn(text: str) -> str:
    """Slack mrkdwn needs only these three escaped; *_~ stay as formatting."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _encode_link_url(url: str) -> str:
    """Only ``|`` and ``>`` would terminate the ``<url|label>`` grammar early."""
    return url.replace("|", "%7C").replace(">", "%3E")


def build_text(message: str, link_url: str | None) -> str:
    body = message
    if len(body) > SLACK_TEXT_LIMIT:
        body = body[: SLACK_TEXT_LIMIT - 1] + "…"
    out = _escape_mrkdwn(body)
    if link_url:
        out = f"{out} <{_encode_link_url(link_url)}|열기>"
    return out


def _warn(summary: str) -> None:
    """Masked, URL-free one-liner to stderr (never the webhook)."""
    sys.stderr.write(f"slack_send: {summary}\n")


def _api_base() -> str:
    """Slack method base; overridable via SLACK_API_BASE for local-fixture tests
    (trailing '/' trimmed). Only the bot (B) path consults this — the webhook (A)
    path POSTs to the full URL stored in the secret."""
    base = os.environ.get("SLACK_API_BASE")
    if base:
        return base.rstrip("/")
    return "https://slack.com/api"


def _post_once(
    url: str, payload: bytes, headers: dict | None = None,
) -> tuple[int, str, str | None]:
    """Return (http_code, body, retry_after). Never surfaces the URL in the
    tuple or in any raised text — urllib's own exceptions carry the URL, so they
    are translated here into a code/body the caller can log safely. ``headers``
    augments the default ``Content-Type: application/json`` (the bot path adds
    ``Authorization``)."""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        url, data=payload, method="POST", headers=hdrs,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            return resp.getcode(), body, None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        retry_after = e.headers.get("Retry-After") if e.headers else None
        return e.code, body, retry_after
    except (urllib.error.URLError, OSError, ValueError) as e:
        # str(e) can embed the URL for some URLError reasons; keep only the
        # reason class so the webhook never leaks into a log.
        reason = getattr(e, "reason", e)
        return 0, f"network-error:{type(reason).__name__}", None


def _post_with_retry(
    url: str, payload: bytes, headers: dict | None = None,
) -> tuple[int, str]:
    """POST once, retrying a single time on HTTP 429 honouring ``Retry-After``
    (capped). Shared by the webhook (A) and bot (B) paths so their rate-limit
    handling is identical. Returns (http_code, body)."""
    code, body, retry_after = _post_once(url, payload, headers)
    if code == 429:
        delay = 1.0
        if retry_after:
            try:
                delay = min(float(retry_after), RETRY_AFTER_CAP_SECONDS)
            except ValueError:
                delay = 1.0
        time.sleep(max(delay, 0.0))
        code, body, _ = _post_once(url, payload, headers)
    return code, body


def _send_webhook(url: str, resolved: str, text: str) -> int:
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    code, body = _post_with_retry(url, payload)
    if code == 200 and body == "ok":
        return 0
    # body is truncated so a verbose Slack error page cannot swamp the log; the
    # URL is deliberately absent from this summary.
    _warn(f"WARN send failed profile={resolved} http={code} body={body[:200]}")
    return 1


def _send_bot(token: str, channel_id: str, resolved: str, text: str) -> int:
    """POST to chat.postMessage with a Bearer token. Slack returns HTTP 200 even
    on failure, so success needs both ``code == 200`` and ``ok is True`` in the
    JSON body. The token and channel ID are credentials and never logged."""
    url = f"{_api_base()}/chat.postMessage"
    payload = json.dumps(
        {"channel": channel_id, "text": text, "unfurl_links": False},
        ensure_ascii=False,
    ).encode("utf-8")
    code, body = _post_with_retry(url, payload, {"Authorization": f"Bearer {token}"})

    ok = False
    slack_error = "?"
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            ok = parsed.get("ok") is True
            slack_error = parsed.get("error") or "?"
        else:
            slack_error = "non_json"
    except ValueError:
        # non-JSON body (empty / HTML error page): keep the summary safe and do
        # not spill the raw body — it never contains the token, but staying terse
        # matches the webhook path's discretion.
        slack_error = "non_json"

    if code == 200 and ok:
        return 0
    _warn(f"WARN bot send failed profile={resolved} http={code} ok=false error={slack_error}")
    return 1


def send(message: str, link_url: str | None = None) -> int:
    """Deliver one message. Returns 0 on success, non-zero on any failure."""
    vault = _vault_root()
    key = _channel_key()
    text = build_text(message, link_url)

    secret = _load_secret(vault)
    token, channels = _load_bot(secret)
    if token:
        channel_id, resolved = _select_channel(channels, key)
        if not channel_id:
            _warn(f"WARN no channel for profile={key} (and no default) — skipped, 0 HTTP")
            return 3
        return _send_bot(token, channel_id, resolved, text)

    webhooks = _load_webhooks(vault)
    url, resolved = _select_webhook(webhooks, key)
    if not url:
        _warn(f"WARN no webhook for profile={key} (and no default) — skipped, 0 HTTP")
        return 3
    return _send_webhook(url, resolved, text)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1]:
        _warn("ERROR usage: slack_send.py \"<message>\" [link_url]")
        return 2
    message = argv[1]
    link_url = argv[2] if len(argv) > 2 and argv[2] else None
    return send(message, link_url)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
