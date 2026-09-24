"""Deliver a local party's call_send into the target Grok Bot agent.

``call_open`` still wakes only the switchboard. A local ``call_send`` posts
to the host gateway's ``deliverAgentMessage``, the same wake Grok Bot agents
use with each other. The message is stored only after the gateway reports
``delivered``. Member sends are stored and not forwarded; the local side is
unchanged.

The gateway returns 403 for any request that carries an Origin header, so
this client never sets one.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

from .db import CallStore, message_with_reply_request
from .directory import resolve_member_agent_id
from .wake import LINK_DOWN_NOTE, notify_link_down

log = logging.getLogger("call_bridge.deliver")

GATEWAY_URL_ENV = "GROKBOT_GATEWAY_URL"
GATEWAY_TOKEN_ENV = "GROKBOT_GATEWAY_TOKEN"
DELIVER_PATH = "/api/deliverAgentMessage"
DELIVER_TIMEOUT_SECONDS = 8
_MAX_BODY = 65_536

_GATEWAY_STATUSES = frozenset(
    {"delivered", "empty", "target_not_found", "not_member", "unavailable"}
)
_PASSTHROUGH_ERRORS = frozenset(
    {"target_not_found", "not_member", "unavailable", "empty"}
)
# Connection failure or timeout. HTTP statuses such as 401 are not this set.
_GATEWAY_TRANSPORT_DETAILS = frozenset({"timed out", "request failed"})


def gateway_link_down_detail(delivery: dict[str, str]) -> str | None:
    """Short error when the gateway forward itself is down.

    ``unavailable`` is the gateway's status for a dead forward. A normal
    delivery status (``target_not_found``, ``not_member``, ``empty``) or an
    HTTP rejection such as 401 is not a down link.
    """
    status = delivery.get("status")
    if status == "unavailable":
        return "unavailable"
    detail = delivery.get("detail") or ""
    if status == "error" and detail in _GATEWAY_TRANSPORT_DETAILS:
        return detail
    return None


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    """Return 3xx/4xx/5xx as responses so Authorization is not replayed."""

    def http_response(self, request, response):  # noqa: ARG002
        return response

    https_response = http_response


def _short(text: str, limit: int = 180) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _scrub(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, "***")
    return text


def gateway_settings() -> tuple[str, str] | str:
    """Return ``(base_url, token)`` or a detail string naming missing env vars.

    The token is never written to logs by this function.
    """
    url = os.environ.get(GATEWAY_URL_ENV, "").strip().rstrip("/")
    token = os.environ.get(GATEWAY_TOKEN_ENV, "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    missing: list[str] = []
    if not url:
        missing.append(GATEWAY_URL_ENV)
    if not token:
        missing.append(GATEWAY_TOKEN_ENV)
    if missing:
        return "unset: " + ", ".join(missing)
    return url, token


def text_for_agent(session_id: str, message: str, reply_required: bool) -> str:
    """Text the bot receives. Same as the stored body when a reply is required."""
    if reply_required:
        return message_with_reply_request(session_id, message, True)
    return (
        f"{message}\n\n"
        f"session_id={session_id} "
        "（返信不要。送る場合は call-bridge MCP の call_send、from_party=member）"
    )


def _http_error_detail(code: int, raw: bytes) -> str:
    if code == 401:
        return "http 401"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"http {code}"
    if not isinstance(data, dict):
        return f"http {code}"
    parts = [f"http {code}"]
    failure = data.get("failureCode")
    message = data.get("message")
    if isinstance(failure, str) and failure.strip():
        parts.append(_short(failure.strip(), 80))
    if isinstance(message, str) and message.strip():
        parts.append(_short(message.strip()))
    return ": ".join(parts)


def deliver_agent_message(
    *,
    base_url: str,
    token: str,
    message_id: str,
    from_id: str,
    from_name: str,
    to_agent_id: str,
    text: str,
) -> dict[str, str]:
    """POST ``/api/deliverAgentMessage``. Never raises. Never logs ``token``."""
    payload = {
        "messageId": message_id,
        "from": {"id": from_id, "name": from_name},
        "toAgentId": to_agent_id,
        "text": text,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # Origin must not be set. urllib does not add it; do not add it here.
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    request = urllib.request.Request(
        base_url + DELIVER_PATH,
        data=body,
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=DELIVER_TIMEOUT_SECONDS) as resp:
            code = int(getattr(resp, "status", 0) or resp.getcode())
            raw = resp.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:
        detail = f"http {exc.code}"
        log.error("deliver failed to=%s: %s", to_agent_id, detail)
        return {"status": "error", "detail": detail}
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError)
        detail = "timed out" if timed_out else "request failed"
        log.error(
            "deliver failed to=%s: %s",
            to_agent_id,
            _scrub(_short(f"{type(exc).__name__}: {exc}"), token),
        )
        return {"status": "error", "detail": detail}

    if not 200 <= code < 300:
        detail = _scrub(_http_error_detail(code, raw), token)
        log.error("deliver failed to=%s: %s", to_agent_id, detail)
        return {"status": "error", "detail": detail}

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "error", "detail": f"http {code}"}
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, str) or status not in _GATEWAY_STATUSES:
        return {"status": "error", "detail": f"http {code}"}
    detail = f"http {code}"
    if status == "delivered":
        log.info("deliver delivered to=%s %s", to_agent_id, detail)
    else:
        log.warning("deliver %s to=%s %s", status, to_agent_id, detail)
    return {"status": status, "detail": detail}


def _delivery_failure(error: str, detail: str, status: str) -> dict[str, Any]:
    return {
        "error": error,
        "detail": detail,
        "delivery": {"status": status, "detail": detail},
    }


def dispatch_send(
    store: CallStore,
    session_id: str,
    from_party: str,
    message: str,
    reply_required: bool = True,
) -> dict[str, Any]:
    """Send a message. Local sends wake the bot; member sends only store.

    Raises ``KeyError`` / ``ValueError`` / ``RuntimeError`` the same way
    ``CallStore.send_message`` does. Gateway and directory failures return
    an ``error`` dict and do not store the local message.
    """
    if from_party not in ("local", "member"):
        raise ValueError("from_party must be 'local' or 'member'")
    if from_party != "local":
        return store.send_message(session_id, from_party, message, reply_required)

    sess = store.get_session(session_id)
    if sess is None:
        raise KeyError(f"session not found: {session_id}")
    if sess["status"] == "hungup":
        raise RuntimeError("session already hung up")

    resolved = resolve_member_agent_id(str(sess.get("member_name") or ""))
    if not resolved.get("ok"):
        error = str(resolved.get("error") or "error")
        detail = str(resolved.get("detail") or "")
        status = "target_not_found" if error == "target_not_found" else "error"
        failure = _delivery_failure(error, detail, status)
        note = resolved.get("note")
        if isinstance(note, str) and note:
            failure["note"] = note
        return failure

    settings = gateway_settings()
    if isinstance(settings, str):
        return _delivery_failure("gateway_not_configured", settings, "error")
    base_url, token = settings

    from_name = (sess.get("local_label") or "").strip() or (
        sess.get("local_id") or ""
    ).strip() or "local"
    text = text_for_agent(session_id, message, reply_required)
    delivery = deliver_agent_message(
        base_url=base_url,
        token=token,
        message_id=str(uuid.uuid4()),
        from_id=f"call-bridge:{session_id}",
        from_name=from_name,
        to_agent_id=str(resolved["id"]),
        text=text,
    )
    status = delivery["status"]
    if status != "delivered":
        error = status if status in _PASSTHROUGH_ERRORS else "error"
        down = gateway_link_down_detail(delivery)
        if down:
            notify_link_down("gateway", down)
            return {
                "error": error,
                "detail": f"{down}. {LINK_DOWN_NOTE}",
                "note": LINK_DOWN_NOTE,
                "delivery": delivery,
            }
        return {
            "error": error,
            "detail": delivery.get("detail", ""),
            "delivery": delivery,
        }

    stored = store.send_message(session_id, "local", message, reply_required)
    stored["delivery"] = delivery
    return stored
