"""Wake the switchboard (Marian).

``session.opened`` is a versioned envelope with no message body. A local
``call_send`` to a GrokBot member posts it as a ring when the member has no
unread message waiting; Marian only tells the member the MCP URL and
``session_id``, and the member reads the text with ``call_poll``. No event
carries the caller's text. ``bridge.link_down`` uses the same webhook, with no
message body, when a request finds the directory unix socket down.
Webhook secrets are never part of any payload.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("call_bridge.wake")

WAKE_SCHEMA = "grokbot.call.v0"
WAKE_EVENT = "session.opened"
DEFAULT_PUBLIC_MCP_URL = "https://call.kitepon.dev/mcp"
WAKE_TIMEOUT_SECONDS = 8

# Distinct from session.opened. Posted at most once per process per interval,
# and only from a request that already hit a down link. No timer is armed.
LINK_DOWN_EVENT = "bridge.link_down"
LINK_DOWN_MIN_INTERVAL_SECONDS = 60
LINK_DOWN_NOTE = (
    "The box link is down. The operator has been woken to reconnect. "
    "Retry in about 30 seconds."
)

_link_down_lock = threading.Lock()
_last_link_down_at: float | None = None

_URL_ENV = (
    "CALL_BRIDGE_WAKE_WEBHOOK_URL",
    "CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL",
)
_AUTH_ENV = (
    "CALL_BRIDGE_WAKE_WEBHOOK_AUTH",
    "CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH",
)


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def public_mcp_url() -> str:
    return os.environ.get("CALL_BRIDGE_PUBLIC_MCP_URL", "").strip() or DEFAULT_PUBLIC_MCP_URL


def build_wake_payload(
    session: dict[str, Any], *, member_agent_id: str | None = None
) -> dict[str, Any]:
    """Stable ``session.opened`` envelope. Allowlisted keys only — no message body.

    ``member_agent_id`` is added when the caller has resolved it, so the
    switchboard can pick the seat by id after a rename.
    """
    payload = {
        "schema": WAKE_SCHEMA,
        "event": WAKE_EVENT,
        "session_id": session.get("session_id"),
        "status": session.get("status") or "ringing",
        "member_name": session.get("member_name"),
        "local_id": session.get("local_id"),
        "local_label": session.get("local_label"),
        "purpose": session.get("purpose"),
        "mcp_url": public_mcp_url(),
        "created_at": session.get("created_at"),
    }
    if member_agent_id:
        payload["member_agent_id"] = member_agent_id
    return payload


class _ReturnHTTPStatus(urllib.request.HTTPErrorProcessor):
    """Return 3xx/4xx/5xx as responses.

    The default processor turns those into errors, and urllib then follows
    POST redirects while copying Authorization. A wake webhook must not
    replay its credential onto another host.
    """

    def http_response(self, request, response):  # noqa: ARG002
        return response

    https_response = http_response


def _short(text: str, limit: int = 180) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _post_json(payload: dict[str, Any]) -> dict[str, str]:
    """POST JSON to the switchboard webhook. Never raises.

    The caller has already confirmed the URL is set. ``detail`` is a short
    status. On a transport error, ``log_detail`` is a short exception string
    for logs. The Authorization value is never included.
    """
    url = _first_env(_URL_ENV)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    auth = _first_env(_AUTH_ENV)
    if auth:
        headers["Authorization"] = auth

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_ReturnHTTPStatus)
    try:
        with opener.open(request, timeout=WAKE_TIMEOUT_SECONDS) as resp:
            code = int(getattr(resp, "status", 0) or resp.getcode())
    except urllib.error.HTTPError as exc:
        return {"status": "error", "detail": f"http {exc.code}"}
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError)
        detail = "timed out" if timed_out else "request failed"
        log_detail = _scrub_secrets(_short(f"{type(exc).__name__}: {exc}"))
        return {
            "status": "error",
            "detail": detail,
            "log_detail": log_detail,
        }

    if 200 <= code < 300:
        return {"status": "ok", "detail": f"http {code}"}
    return {"status": "error", "detail": f"http {code}"}


def _log_notify(result: dict[str, str], ok_fmt: str, err_fmt: str, *args: object) -> dict[str, str]:
    log_detail = result.pop("log_detail", result["detail"])
    if result["status"] == "ok":
        log.info(ok_fmt, *args, result["detail"])
    else:
        log.error(err_fmt, *args, log_detail)
    return result


def notify_ring(session: dict[str, Any], *, member_agent_id: str) -> dict[str, str]:
    """POST ``session.opened`` as the ring for a local ``call_send``. Never raises.

    The ``session.opened`` envelope with ``member_agent_id``; no message
    body. A missing webhook URL is an error here: local ``call_send`` must not
    store the message or report it delivered when this is not ``ok``.
    A failed POST is not a box-link failure and does not send ``bridge.link_down``.
    Returns ``{"status": "ok"|"error", "detail": "..."}``.
    """
    session_id = session.get("session_id")
    url = _first_env(_URL_ENV)
    if not url:
        log.error("ring failed session_id=%s: webhook url unset", session_id)
        return {"status": "error", "detail": "webhook url unset"}

    result = _post_json(build_wake_payload(session, member_agent_id=member_agent_id))
    return _log_notify(
        result,
        "ring ok session_id=%s %s",
        "ring failed session_id=%s: %s",
        session_id,
    )


def _scrub_secrets(text: str) -> str:
    """Remove webhook and gateway credentials if a detail string ever holds them."""
    secrets: list[str] = []
    for name in (*_AUTH_ENV, "GROKBOT_GATEWAY_TOKEN"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        secrets.append(raw)
        if raw.lower().startswith("bearer "):
            token = raw[7:].strip()
            if token:
                secrets.append(token)
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


def build_link_down_payload(link: str, detail: str) -> dict[str, str]:
    """Switchboard envelope for a down box link. No session and no secrets."""
    safe = _scrub_secrets(_short(detail)) or "request failed"
    return {
        "event": LINK_DOWN_EVENT,
        "link": link,
        "detail": safe,
    }


def reset_link_down_limiter() -> None:
    """Clear the in-process interval. Tests only; production has no timer."""
    global _last_link_down_at
    with _link_down_lock:
        _last_link_down_at = None


def notify_link_down(link: str, detail: str) -> dict[str, str]:
    """POST ``bridge.link_down`` at most once per 60 seconds. Never raises.

    Uses the same webhook URL and Authorization value as ``notify_ring``.
    A skipped POST because the URL is unset does not start the interval.
    There is no background retry.
    """
    global _last_link_down_at
    url = _first_env(_URL_ENV)
    if not url:
        log.info("link-down wake skipped: webhook URL unset (link=%s)", link)
        return {"status": "skipped", "detail": "webhook url unset"}

    now = time.monotonic()
    with _link_down_lock:
        last = _last_link_down_at
        if last is not None and now - last < LINK_DOWN_MIN_INTERVAL_SECONDS:
            log.info("link-down wake skipped: rate limited (link=%s)", link)
            return {"status": "skipped", "detail": "rate limited"}
        _last_link_down_at = now

    result = _post_json(build_link_down_payload(link, detail))
    return _log_notify(
        result,
        "link-down wake ok link=%s %s",
        "link-down wake failed link=%s: %s",
        link,
    )
