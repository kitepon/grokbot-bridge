"""Deliver a local party's call_send.

A local ``call_send`` to a GrokBot member stores the text in the bridge and
rings the member through Marian's webhook (``session.opened``, no message
body). Marian only passes the MCP URL and ``session_id``; the member reads the
text with ``call_poll``. While an earlier message is still unread a ring is
already outstanding, so further sends are stored without another ring until
``CALL_BRIDGE_RERING_SECONDS`` has passed. A needed ring that does not return
HTTP 2xx fails the send and nothing is stored. Member sends are stored and not
forwarded.

This path does not call the host gateway ``deliverAgentMessage``. A down
directory unix socket still posts ``bridge.link_down`` from the directory
read, before the ring.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from .db import CallStore
from .directory import resolve_member_agent_id
from .bellteam import BellTeamError, BellTeamOutcomeUnknown, send_delivery
from .wake import notify_ring

log = logging.getLogger("call_bridge.deliver")

DEFAULT_RERING_SECONDS = 600


def _rering_seconds() -> float:
    raw = os.environ.get("CALL_BRIDGE_RERING_SECONDS", "").strip()
    try:
        value = float(raw) if raw else float(DEFAULT_RERING_SECONDS)
    except ValueError:
        return float(DEFAULT_RERING_SECONDS)
    return value if value >= 0 else float(DEFAULT_RERING_SECONDS)


def _ring_needed(unread: dict[str, Any]) -> bool:
    """Ring unless an earlier, recent message is still waiting for the member's poll."""
    if not unread["count"]:
        return True
    try:
        oldest = datetime.fromisoformat(str(unread["oldest_created_at"]))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - oldest).total_seconds() >= _rering_seconds()


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
    """Send a message. Local sends ring the member via Marian; member sends only store.

    Raises ``KeyError`` / ``ValueError`` / ``RuntimeError`` the same way
    ``CallStore.send_message`` does. Directory and ring failures return
    an ``error`` dict and do not store the local message.
    """
    if from_party not in ("local", "member"):
        raise ValueError("from_party must be 'local' or 'member'")
    if from_party != "local":
        stored = store.send_message(session_id, from_party, message, reply_required)
        sess = store.get_session(session_id)
        if sess and sess.get("local_system") == "bellteam":
            try:
                send_delivery({
                    "schema": "call-bridge.delivery.v1", "event": "session.reply",
                    "session_id": session_id, "seq": stored["seq"],
                    "target_id": sess["local_id"],
                    "source_system": sess.get("member_system") or "grokbot",
                    "source_id": sess.get("member_id") or sess["member_name"],
                    "source_label": sess["member_name"],
                    "message": message, "reply_required": reply_required,
                })
                stored["delivery"] = {"status": "delivered", "detail": "BellTeam accepted"}
            except BellTeamOutcomeUnknown as exc:
                stored["delivery"] = {"status": "unknown", "detail": str(exc)}
            except BellTeamError as exc:
                stored["delivery"] = {"status": "error", "detail": str(exc)}
        return stored

    sess = store.get_session(session_id)
    if sess is None:
        raise KeyError(f"session not found: {session_id}")
    if sess["status"] == "hungup":
        raise RuntimeError("session already hung up")

    if sess.get("member_system") == "bellteam":
        member_id = sess.get("member_id")
        if not member_id:
            return _delivery_failure("target_not_found", "BellTeam member id missing", "target_not_found")
        try:
            send_delivery({
                "schema": "call-bridge.delivery.v1", "event": "session.message",
                "session_id": session_id, "target_id": member_id,
                "source_system": sess.get("local_system") or "local",
                "source_id": sess["local_id"], "source_label": sess["local_label"],
                "message": message, "reply_required": reply_required,
            })
        except BellTeamOutcomeUnknown as exc:
            stored = store.send_message(session_id, "local", message, reply_required)
            stored["delivery"] = {"status": "unknown", "detail": str(exc)}
            return stored
        except BellTeamError as exc:
            return _delivery_failure("error", str(exc), "error")
        stored = store.send_message(session_id, "local", message, reply_required)
        stored["delivery"] = {"status": "delivered", "detail": "BellTeam accepted"}
        return stored

    resolved = {"ok": True, "id": sess["member_id"]} if sess.get("member_id") else resolve_member_agent_id(str(sess.get("member_name") or ""))
    if not resolved.get("ok"):
        error = str(resolved.get("error") or "error")
        detail = str(resolved.get("detail") or "")
        status = "target_not_found" if error == "target_not_found" else "error"
        failure = _delivery_failure(error, detail, status)
        note = resolved.get("note")
        if isinstance(note, str) and note:
            failure["note"] = note
        return failure

    if not _ring_needed(store.unread_by_member(session_id)):
        stored = store.send_message(session_id, "local", message, reply_required)
        stored["delivery"] = {"status": "delivered", "detail": "queued behind an earlier ring"}
        return stored

    rung = notify_ring(sess, member_agent_id=str(resolved["id"]))
    if rung["status"] != "ok":
        detail = str(rung.get("detail") or "")
        if detail == "webhook url unset":
            log.info("local send not stored: webhook url unset session_id=%s", session_id)
            return _delivery_failure("webhook_not_configured", detail, "error")
        return _delivery_failure("error", detail, "error")

    stored = store.send_message(session_id, "local", message, reply_required)
    stored["delivery"] = {"status": "delivered", "detail": rung.get("detail", "")}
    return stored
