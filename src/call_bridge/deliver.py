"""Deliver a local party's call_send by waking Marian.

``call_open`` posts ``session.opened`` with no message body. A local
``call_send`` resolves the member agent id, then posts ``session.message``
(including the caller's text) to the same switchboard webhook. Marian relays
that into the member's main chat. The message is stored only after the webhook
returns HTTP 2xx. Member sends are stored and not forwarded.

This path does not call the host gateway ``deliverAgentMessage``. A down
directory unix socket still posts ``bridge.link_down`` from the directory
read, before this relay.
"""

from __future__ import annotations

import logging
from typing import Any

from .db import CallStore
from .directory import resolve_member_agent_id
from .wake import notify_message

log = logging.getLogger("call_bridge.deliver")


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
    """Send a message. Local sends wake Marian; member sends only store.

    Raises ``KeyError`` / ``ValueError`` / ``RuntimeError`` the same way
    ``CallStore.send_message`` does. Directory and webhook failures return
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

    notified = notify_message(
        sess,
        member_agent_id=str(resolved["id"]),
        message=message,
        reply_required=reply_required,
    )
    if notified["status"] != "ok":
        detail = str(notified.get("detail") or "")
        if detail == "webhook url unset":
            log.info("local send not stored: webhook url unset session_id=%s", session_id)
            return _delivery_failure("webhook_not_configured", detail, "error")
        return _delivery_failure("error", detail, "error")

    stored = store.send_message(session_id, "local", message, reply_required)
    stored["delivery"] = {"status": "delivered", "detail": notified.get("detail", "")}
    return stored
