"""Payload shape and wake-notify behavior (stdlib only, no MCP import)."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from call_bridge.wake import (  # noqa: E402
    DEFAULT_PUBLIC_MCP_URL,
    MESSAGE_EVENT,
    WAKE_TIMEOUT_SECONDS,
    build_message_payload,
    build_wake_payload,
    notify_message,
    notify_wake,
)

_ENV_KEYS = (
    "CALL_BRIDGE_WAKE_WEBHOOK_URL",
    "CALL_BRIDGE_WAKE_WEBHOOK_AUTH",
    "CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL",
    "CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH",
    "CALL_BRIDGE_PUBLIC_MCP_URL",
)


def _session(**overrides: object) -> dict:
    base = {
        "session_id": "sess-1",
        "status": "ringing",
        "member_name": "ラピ",
        "local_id": "local-1",
        "local_label": "cursor",
        "purpose": "check the deploy",
        "created_at": "2026-09-24T00:00:00+00:00",
        "body": "conversation that must not be forwarded",
        "message": "nope",
        "messages": [{"body": "nope"}],
    }
    base.update(overrides)
    return base


class _CaptureHandler(BaseHTTPRequestHandler):
    status_code = 204
    location: str | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.captured = {  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": body,
        }
        self.send_response(self.status_code)
        if self.location:
            self.send_header("Location", self.location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


class WakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_payload_is_allowlisted_and_has_no_message_body(self) -> None:
        payload = build_wake_payload(_session(purpose=None))
        self.assertEqual(
            set(payload),
            {
                "schema",
                "event",
                "session_id",
                "status",
                "member_name",
                "local_id",
                "local_label",
                "purpose",
                "mcp_url",
                "created_at",
            },
        )
        self.assertEqual(payload["schema"], "grokbot.call.v0")
        self.assertEqual(payload["event"], "session.opened")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["status"], "ringing")
        self.assertEqual(payload["member_name"], "ラピ")
        self.assertIsNone(payload["purpose"])
        self.assertEqual(payload["mcp_url"], DEFAULT_PUBLIC_MCP_URL)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("conversation", encoded)
        self.assertNotIn("nope", encoded)

    def test_public_mcp_url_override(self) -> None:
        os.environ["CALL_BRIDGE_PUBLIC_MCP_URL"] = " https://example.test/mcp "
        payload = build_wake_payload(_session())
        self.assertEqual(payload["mcp_url"], "https://example.test/mcp")

    def test_blank_public_mcp_url_uses_default(self) -> None:
        os.environ["CALL_BRIDGE_PUBLIC_MCP_URL"] = "   "
        self.assertEqual(build_wake_payload(_session())["mcp_url"], DEFAULT_PUBLIC_MCP_URL)

    def test_skip_when_url_unset(self) -> None:
        result = notify_wake(_session())
        self.assertEqual(result, {"status": "skipped", "detail": "webhook url unset"})

    def test_skip_when_url_blank_and_alias_blank(self) -> None:
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = "  "
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL"] = ""
        result = notify_wake(_session())
        self.assertEqual(result["status"], "skipped")

    def test_timeout_is_short(self) -> None:
        self.assertGreaterEqual(WAKE_TIMEOUT_SECONDS, 5)
        self.assertLessEqual(WAKE_TIMEOUT_SECONDS, 10)

    def test_post_uses_primary_url_auth_and_payload(self) -> None:
        server = self._serve(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL"] = "http://127.0.0.1:9/alias-must-not-win"
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = "Bearer test-token"
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH"] = "Bearer alias-token"

        result = notify_wake(_session(purpose=None))

        self.assertEqual(result, {"status": "ok", "detail": "http 204"})
        req = server.captured  # type: ignore[attr-defined]
        self.assertIsNotNone(req)
        self.assertEqual(req["authorization"], "Bearer test-token")
        self.assertEqual(req["content_type"], "application/json")
        body = json.loads(req["body"].decode("utf-8"))
        self.assertEqual(body["schema"], "grokbot.call.v0")
        self.assertEqual(body["event"], "session.opened")
        self.assertEqual(body["member_name"], "ラピ")
        self.assertIsNone(body["purpose"])
        self.assertNotIn("body", body)
        self.assertNotIn("message", body)
        self.assertNotIn("messages", body)

    def test_alias_url_and_auth_when_primary_unset(self) -> None:
        server = self._serve(200)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH"] = "Bearer alias-token"

        result = notify_wake(_session())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["detail"], "http 200")
        req = server.captured  # type: ignore[attr-defined]
        self.assertEqual(req["authorization"], "Bearer alias-token")

    def test_omits_authorization_when_auth_unset(self) -> None:
        server = self._serve(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)

        result = notify_wake(_session())

        self.assertEqual(result["status"], "ok")
        req = server.captured  # type: ignore[attr-defined]
        self.assertIsNone(req["authorization"])

    def test_http_error_is_nonfatal(self) -> None:
        server = self._serve(502)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)

        result = notify_wake(_session())

        self.assertEqual(result, {"status": "error", "detail": "http 502"})

    def test_redirect_is_not_followed(self) -> None:
        bait = self._serve(204)
        server = self._serve(302, location=self._url(bait))
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = "Bearer secret"

        result = notify_wake(_session())

        self.assertEqual(result, {"status": "error", "detail": "http 302"})
        self.assertIsNone(bait.captured)  # type: ignore[attr-defined]

    def test_message_payload_includes_body_and_agent_id(self) -> None:
        payload = build_message_payload(
            _session(purpose="secret purpose"),
            member_agent_id="agent-1",
            message="状況を教えてください",
            reply_required=True,
        )
        self.assertEqual(
            set(payload),
            {
                "schema",
                "event",
                "session_id",
                "member_name",
                "member_agent_id",
                "local_id",
                "local_label",
                "message",
                "reply_required",
                "mcp_url",
            },
        )
        self.assertEqual(payload["schema"], "grokbot.call.v0")
        self.assertEqual(payload["event"], MESSAGE_EVENT)
        self.assertEqual(MESSAGE_EVENT, "session.message")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["member_name"], "ラピ")
        self.assertEqual(payload["member_agent_id"], "agent-1")
        self.assertEqual(payload["local_id"], "local-1")
        self.assertEqual(payload["local_label"], "cursor")
        self.assertEqual(payload["message"], "状況を教えてください")
        self.assertIs(payload["reply_required"], True)
        self.assertEqual(payload["mcp_url"], DEFAULT_PUBLIC_MCP_URL)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret purpose", encoded)
        self.assertNotIn("conversation", encoded)
        self.assertNotIn("purpose", payload)
        self.assertNotIn("status", payload)
        self.assertNotIn("created_at", payload)

    def test_message_payload_keeps_reply_not_required(self) -> None:
        payload = build_message_payload(
            _session(),
            member_agent_id="agent-1",
            message="共有だけです",
            reply_required=False,
        )
        self.assertIs(payload["reply_required"], False)
        self.assertEqual(payload["message"], "共有だけです")

    def test_message_notify_errors_when_url_unset(self) -> None:
        result = notify_message(
            _session(), member_agent_id="agent-1", message="hi", reply_required=True
        )
        self.assertEqual(result, {"status": "error", "detail": "webhook url unset"})

    def test_message_notify_errors_when_url_blank(self) -> None:
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = "  "
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL"] = ""
        result = notify_message(
            _session(), member_agent_id="agent-1", message="hi", reply_required=False
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["detail"], "webhook url unset")

    def test_message_post_includes_body_and_not_the_auth_secret(self) -> None:
        server = self._serve(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL"] = "http://127.0.0.1:9/alias-must-not-win"
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = "Bearer test-token"
        os.environ["CALL_BRIDGE_PUBLIC_MCP_URL"] = "https://example.test/mcp"

        result = notify_message(
            _session(),
            member_agent_id="rapi-agent",
            message="状況を教えてください",
            reply_required=True,
        )

        self.assertEqual(result, {"status": "ok", "detail": "http 204"})
        req = server.captured  # type: ignore[attr-defined]
        self.assertEqual(req["authorization"], "Bearer test-token")
        self.assertEqual(req["content_type"], "application/json")
        body = json.loads(req["body"].decode("utf-8"))
        self.assertEqual(body["event"], "session.message")
        self.assertEqual(body["message"], "状況を教えてください")
        self.assertIs(body["reply_required"], True)
        self.assertEqual(body["member_agent_id"], "rapi-agent")
        self.assertEqual(body["mcp_url"], "https://example.test/mcp")
        self.assertNotIn("test-token", req["body"].decode("utf-8"))
        self.assertNotIn("Bearer", req["body"].decode("utf-8"))
        self.assertNotIn("purpose", body)
        self.assertNotIn("body", body)

    def test_message_alias_url_when_primary_unset(self) -> None:
        server = self._serve(200)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH"] = "Bearer alias-token"

        result = notify_message(
            _session(), member_agent_id="agent-1", message="hello", reply_required=False
        )

        self.assertEqual(result, {"status": "ok", "detail": "http 200"})
        req = server.captured  # type: ignore[attr-defined]
        self.assertEqual(req["authorization"], "Bearer alias-token")
        body = json.loads(req["body"].decode("utf-8"))
        self.assertIs(body["reply_required"], False)
        self.assertNotIn("alias-token", req["body"].decode("utf-8"))

    def test_message_http_error(self) -> None:
        server = self._serve(502)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        result = notify_message(
            _session(), member_agent_id="agent-1", message="hello", reply_required=True
        )
        self.assertEqual(result, {"status": "error", "detail": "http 502"})

    def test_message_redirect_is_not_followed(self) -> None:
        bait = self._serve(204)
        server = self._serve(302, location=self._url(bait))
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = "Bearer secret"

        result = notify_message(
            _session(), member_agent_id="agent-1", message="hello", reply_required=True
        )

        self.assertEqual(result, {"status": "error", "detail": "http 302"})
        self.assertIsNone(bait.captured)  # type: ignore[attr-defined]

    def test_message_connection_failure(self) -> None:
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = f"http://127.0.0.1:{port}/wake"
        result = notify_message(
            _session(), member_agent_id="agent-1", message="hello", reply_required=True
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["detail"], "request failed")

    def test_connection_failure_is_nonfatal(self) -> None:
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = f"http://127.0.0.1:{port}/wake"
        result = notify_wake(_session())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["detail"], "request failed")

    def _serve(self, status: int, location: str | None = None) -> ThreadingHTTPServer:
        handler = type(
            f"Handler{status}{id(self)}",
            (_CaptureHandler,),
            {"status_code": status, "location": location},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.captured = None  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def _url(server: ThreadingHTTPServer) -> str:
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/wake"


if __name__ == "__main__":
    unittest.main()
