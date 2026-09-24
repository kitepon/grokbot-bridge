"""Wake the switchboard when a call session opens.

The switchboard (Marian) is wake-only. This POST is a versioned envelope with
enough to ring the member: session id, names, purpose, and the public MCP URL.
Conversation message bodies are never included.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("call_bridge.wake")

WAKE_SCHEMA = "grokbot.call.v0"
WAKE_EVENT = "session.opened"
DEFAULT_PUBLIC_MCP_URL = "https://call.kitepon.dev/mcp"
WAKE_TIMEOUT_SECONDS = 8

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


def build_wake_payload(session: dict[str, Any]) -> dict[str, Any]:
    """Stable envelope. Allowlisted keys only — never message bodies."""
    return {
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


def notify_wake(session: dict[str, Any]) -> dict[str, str]:
    """POST the wake envelope. Never raises.

    Returns ``{"status": "ok"|"skipped"|"error", "detail": "..."}``.
    Opening a call must not fail because this notification failed.
    """
    session_id = session.get("session_id")
    url = _first_env(_URL_ENV)
    if not url:
        log.info("wake notify skipped: webhook URL unset (session_id=%s)", session_id)
        return {"status": "skipped", "detail": "webhook url unset"}

    payload = build_wake_payload(session)
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
        detail = f"http {exc.code}"
        log.error("wake notify failed session_id=%s: %s", session_id, detail)
        return {"status": "error", "detail": detail}
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError)
        detail = "timed out" if timed_out else "request failed"
        log.error(
            "wake notify failed session_id=%s: %s",
            session_id,
            _short(f"{type(exc).__name__}: {exc}"),
        )
        return {"status": "error", "detail": detail}

    if 200 <= code < 300:
        detail = f"http {code}"
        log.info("wake notify ok session_id=%s %s", session_id, detail)
        return {"status": "ok", "detail": detail}

    detail = f"http {code}"
    log.error("wake notify failed session_id=%s: %s", session_id, detail)
    return {"status": "error", "detail": detail}
