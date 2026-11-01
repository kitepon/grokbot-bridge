"""Deliver a local party's call_send.

GrokBot delivery is selected with ``CALL_BRIDGE_GROKBOT_DELIVERY``:

* ``courier`` (default) stores the text and leaves it for the box courier.
  The courier POSTs the gateway ``sendPrompt`` on the box and reports the
  outcome with ``call_receipt``. Nothing is sent to Marian.
* ``marian`` keeps the previous ring: store the text and POST ``session.opened``
  with no message body when the member has nothing unread. The member reads
  the text with ``call_poll``. A needed ring that does not return HTTP 2xx
  fails the send and nothing is stored.

Every local message, GrokBot or BellTeam, gets the sender preface first.
BellTeam still goes out through its UNIX socket. A down directory unix socket
still posts ``bridge.link_down`` from the directory read, except when a fresh
courier push already supplied the book.

This path does not call the host gateway ``deliverAgentMessage``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from .bellteam import BellTeamError, BellTeamOutcomeUnknown, message_payload, reply_payload, send_delivery
from .db import CallStore
from .directory import resolve_member_agent_id
from .sender import with_sender_preface
from .wake import notify_ring

log = logging.getLogger("call_bridge.deliver")

DEFAULT_RERING_SECONDS = 600


def grokbot_delivery_mode() -> str:
    """``courier`` delivers by sendPrompt. ``marian`` is the ring fallback."""
    raw = os.environ.get("CALL_BRIDGE_GROKBOT_DELIVERY", "courier").strip().lower()
    if raw in ("courier", "marian"):
        return raw
    return "courier"


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
    sender_name: str | None = None,
    sender_team: str | None = None,
) -> dict[str, Any]:
    """Send a message. Local sends name the sender, then deliver.

    Raises ``KeyError`` / ``ValueError`` / ``RuntimeError`` the same way
    ``CallStore.send_message`` does. A missing sender, an unknown member,
    or a failed Marian ring returns an ``error`` dict and does not store
    the local message. Courier mode stores first and returns ``queued``.
    """
    if from_party not in ("local", "member"):
        raise ValueError("from_party must be 'local' or 'member'")
    if from_party == "local":
        name = (sender_name or "").strip()
        team = (sender_team or "").strip()
        if not name or not team:
            return {
                "error": "rejected",
                "detail": "sender_name and sender_team are required",
            }
        message = with_sender_preface(message, team, name)
    if from_party != "local":
        stored = store.send_message(session_id, from_party, message, reply_required)
        sess = store.get_session(session_id)
        if sess and sess.get("local_system") == "bellteam":
            try:
                send_delivery(reply_payload(sess, stored["seq"], message, reply_required))
                stored["delivery"] = {"status": "delivered", "detail": "BellTeam accepted"}
            except BellTeamOutcomeUnknown as exc:
                stored["delivery"] = {"status": "unknown", "detail": str(exc)}
            except BellTeamError as exc:
                stored["delivery"] = {"status": "error", "detail": str(exc)}
        elif sess:
            # Other callers fetch by polling. Say so, with the last time they asked,
            # so the sender does not read "stored" as "received".
            seen = sess.get("local_seen_at")
            stored["delivery"] = {
                "status": "stored",
                "detail": f"waiting for the caller to fetch; last fetch at {seen}" if seen
                else "waiting for the caller to fetch; it has not fetched on this call yet",
                "local_seen_at": seen,
            }
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
            send_delivery(message_payload(sess, message, reply_required))
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

    if grokbot_delivery_mode() == "courier":
        stored = store.send_message(
            session_id, "local", message, reply_required, grokbot_outbox=True
        )
        stored["delivery"] = {
            "status": "queued",
            "detail": "the box courier will deliver this directly",
        }
        return stored

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
