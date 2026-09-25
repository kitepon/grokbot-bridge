"""Local call_send wakes Marian's webhook with the message body."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from call_bridge.db import CallStore, message_with_reply_request  # noqa: E402
from call_bridge.deliver import dispatch_send  # noqa: E402

_ENV_KEYS = (
    "GROKBOT_GATEWAY_URL",
    "GROKBOT_GATEWAY_TOKEN",
    "CALL_BRIDGE_WAKE_WEBHOOK_URL",
    "CALL_BRIDGE_WAKE_WEBHOOK_AUTH",
    "CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL",
    "CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH",
    "CALL_BRIDGE_PUBLIC_MCP_URL",
    "CALL_BRIDGE_DIRECTORY_UNIX",
    "CALL_BRIDGE_DIRECTORY_UNIX_PATH",
    "CALL_BRIDGE_DIRECTORY_URL",
    "CALL_BRIDGE_DIRECTORY_URL_AUTH",
    "CALL_BRIDGE_DIRECTORY_URL_TIMEOUT",
    "CALL_BRIDGE_AGENTS_ROOT",
    "CALL_BRIDGE_DIRECTORY",
)

_MISSING_AGENTS = "/nonexistent/call-bridge-agents-root"
_MISSING_SNAPSHOT = "/nonexistent/call-bridge-directory.json"
_AUTH = "Bearer wake-secret-do-not-log"
_GATEWAY_TOKEN = "gw-token-do-not-log"


class _WebhookHandler(BaseHTTPRequestHandler):
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


class _GatewayHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.captured = {  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        }
        raw = b'{"status":"delivered"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _DirectoryHandler(BaseHTTPRequestHandler):
    payload: dict = {"members": []}

    def do_GET(self) -> None:  # noqa: N802
        raw = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class DeliverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = _MISSING_AGENTS
        os.environ["CALL_BRIDGE_DIRECTORY"] = _MISSING_SNAPSHOT
        self.temp = tempfile.TemporaryDirectory()
        self.store = CallStore(Path(self.temp.name) / "calls.sqlite")
        self.logs = _ListHandler()
        for name in ("call_bridge.deliver", "call_bridge.wake"):
            logging.getLogger(name).addHandler(self.logs)
            logging.getLogger(name).setLevel(logging.DEBUG)

    def tearDown(self) -> None:
        for name in ("call_bridge.deliver", "call_bridge.wake"):
            logging.getLogger(name).removeHandler(self.logs)
        self.temp.cleanup()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_mcp_call_send_delegates_to_dispatch(self) -> None:
        from call_bridge import server

        sentinel = {"delivery": {"status": "delivered", "detail": "http 204"}}
        with mock.patch.object(server, "dispatch_send", return_value=sentinel) as dispatch:
            result = server.call_send("sess", "local", "hello", False)
        dispatch.assert_called_once_with(server.store, "sess", "local", "hello", False)
        self.assertEqual(result, sentinel)

    def test_local_send_posts_session_message_and_stores(self) -> None:
        webhook = self._webhook(204)
        gateway = self._gateway()
        self._use_webhook(webhook)
        self._use_gateway(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ", "check")

        result = dispatch_send(self.store, session["session_id"], "local", "状況を教えてください")

        self.assertNotIn("error", result)
        self.assertEqual(result["delivery"], {"status": "delivered", "detail": "http 204"})
        self.assertEqual(result["status"], "open")
        stored_text = message_with_reply_request(
            session["session_id"], "状況を教えてください", True
        )
        self.assertEqual(result["message"], stored_text)
        req = self._captured(webhook)
        self.assertEqual(req["path"], "/wake")
        self.assertEqual(req["authorization"], _AUTH)
        self.assertEqual(req["content_type"], "application/json")
        body = json.loads(req["body"].decode("utf-8"))
        self.assertEqual(body["schema"], "grokbot.call.v0")
        self.assertEqual(body["event"], "session.message")
        self.assertEqual(body["session_id"], session["session_id"])
        self.assertEqual(body["member_name"], "ラピ")
        self.assertEqual(body["member_agent_id"], "rapi-agent")
        self.assertEqual(body["local_id"], "local-1")
        self.assertEqual(body["local_label"], "Cursor")
        self.assertEqual(body["message"], "状況を教えてください")
        self.assertIs(body["reply_required"], True)
        self.assertNotIn("状況を教えてください\n", body["message"])
        self.assertNotIn("wake-secret-do-not-log", req["body"].decode("utf-8"))
        self.assertNotIn("gw-token-do-not-log", req["body"].decode("utf-8"))
        stored = self.store.poll_messages(session["session_id"], "member", mark_delivered=False)
        self.assertEqual([item["message"] for item in stored["messages"]], [stored_text])
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]
        joined = "\n".join(self.logs.messages)
        self.assertNotIn("wake-secret-do-not-log", joined)
        self.assertNotIn("gw-token-do-not-log", joined)
        self.assertNotIn("状況を教えてください", joined)

    def test_local_send_does_not_require_gateway_env(self) -> None:
        webhook = self._webhook(200)
        self._use_webhook(webhook)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "hello")

        self.assertEqual(result["delivery"], {"status": "delivered", "detail": "http 200"})
        self.assertIsNone(os.environ.get("GROKBOT_GATEWAY_URL"))
        self.assertIsNone(os.environ.get("GROKBOT_GATEWAY_TOKEN"))
        body = json.loads(self._captured(webhook)["body"].decode("utf-8"))
        self.assertEqual(body["event"], "session.message")
        self.assertEqual(body["member_agent_id"], "rapi-agent")

    def test_notice_sets_reply_required_false(self) -> None:
        webhook = self._webhook(204)
        self._use_webhook(webhook)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(
            self.store, session["session_id"], "local", "共有だけです", reply_required=False
        )

        self.assertEqual(result["message"], "共有だけです")
        body = json.loads(self._captured(webhook)["body"].decode("utf-8"))
        self.assertEqual(body["message"], "共有だけです")
        self.assertIs(body["reply_required"], False)
        self.assertNotIn("返信不要", body["message"])
        self.assertNotIn("返信が必要です", body["message"])

    def test_member_send_does_not_call_the_webhook(self) -> None:
        webhook = self._webhook(204)
        self._use_webhook(webhook)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "member", "確認しました")

        self.assertEqual(result["message"], "確認しました")
        self.assertNotIn("delivery", result)
        self.assertIsNone(webhook.captured)  # type: ignore[attr-defined]

    def test_missing_webhook_env_does_not_store(self) -> None:
        gateway = self._gateway()
        self._use_gateway(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "届かない")

        self.assertEqual(result["error"], "webhook_not_configured")
        self.assertEqual(result["detail"], "webhook url unset")
        self.assertEqual(result["delivery"]["status"], "error")
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertEqual(self.store.get_session(session["session_id"])["status"], "ringing")
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]

    def test_remote_directory_id_is_the_target(self) -> None:
        webhook = self._webhook(204)
        self._use_webhook(webhook)
        directory = self._directory({
            "ok": True,
            "source": "agent-profiles",
            "members": [{"name": "ラピ", "id": "  live-agent-id  ", "title": "インフラ統括"}],
        })
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(directory)
        self._profiles({"local-folder-must-not-win": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        dispatch_send(self.store, session["session_id"], "local", "hello")

        body = json.loads(self._captured(webhook)["body"].decode("utf-8"))
        self.assertEqual(body["member_agent_id"], "live-agent-id")
        self.assertEqual(body["event"], "session.message")

    def test_remote_agent_id_field_and_id_fallback(self) -> None:
        webhook = self._webhook(204)
        self._use_webhook(webhook)
        directory = self._directory({
            "members": [{"name": "ラピ", "agentId": "from-agent-id"}],
        })
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(directory)
        by_name = self.store.open_session("local-1", "Cursor", "ラピ")
        dispatch_send(self.store, by_name["session_id"], "local", "hello")
        first = json.loads(self._captured(webhook)["body"].decode("utf-8"))
        self.assertEqual(first["member_agent_id"], "from-agent-id")

        by_id = self.store.open_session("local-1", "", "from-agent-id")
        result = dispatch_send(self.store, by_id["session_id"], "local", "hello")
        self.assertEqual(result["delivery"]["status"], "delivered")
        second = json.loads(self._captured(webhook)["body"].decode("utf-8"))
        self.assertEqual(second["member_agent_id"], "from-agent-id")
        self.assertEqual(second["local_label"], "")
        self.assertEqual(second["local_id"], "local-1")

    def test_snapshot_without_id_does_not_store_or_call_webhook(self) -> None:
        webhook = self._webhook(204)
        self._use_webhook(webhook)
        path = Path(self.temp.name) / "directory.json"
        path.write_text(
            json.dumps({"members": [{"name": "ラピ", "title": "インフラ統括"}]}),
            encoding="utf-8",
        )
        os.environ["CALL_BRIDGE_DIRECTORY"] = str(path)
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "hello")

        self.assertEqual(result["error"], "target_not_found")
        self.assertIn("no agent id", result["detail"])
        self.assertEqual(result["delivery"]["status"], "target_not_found")
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertIsNone(webhook.captured)  # type: ignore[attr-defined]

    def test_unknown_member_and_ambiguous_id(self) -> None:
        webhook = self._webhook(204)
        self._use_webhook(webhook)
        self._profiles({"one": "ラピ"})
        missing_session = self.store.open_session("local-1", "Cursor", "いない人")

        missing = dispatch_send(self.store, missing_session["session_id"], "local", "hello")
        self.assertEqual(missing["error"], "target_not_found")
        self.assertIn("no directory entry", missing["detail"])
        self.assertIsNone(webhook.captured)  # type: ignore[attr-defined]

        directory = self._directory({
            "members": [
                {"name": "ラピ", "id": "a"},
                {"name": "ラピ", "id": "b"},
            ],
        })
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(directory)
        session = self.store.open_session("local-1", "Cursor", "ラピ")
        ambiguous = dispatch_send(self.store, session["session_id"], "local", "hello")
        self.assertEqual(ambiguous["error"], "target_not_found")
        self.assertIn("multiple agent ids", ambiguous["detail"])
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertIsNone(webhook.captured)  # type: ignore[attr-defined]

    def test_http_error_does_not_store(self) -> None:
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")
        denied = self._webhook(502)
        self._use_webhook(denied)
        result = dispatch_send(self.store, session["session_id"], "local", "hello")
        self.assertEqual(result["error"], "error")
        self.assertEqual(result["detail"], "http 502")
        self.assertEqual(result["delivery"], {"status": "error", "detail": "http 502"})
        self.assertNotIn("wake-secret-do-not-log", result["detail"])
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertNotIn("wake-secret-do-not-log", "\n".join(self.logs.messages))

    def test_redirect_is_not_followed(self) -> None:
        bait = self._webhook(204)
        webhook = self._webhook(302, location=self._url(bait) + "/wake")
        self._use_webhook(webhook)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "hello")

        self.assertEqual(result["error"], "error")
        self.assertEqual(result["detail"], "http 302")
        self.assertIsNone(bait.captured)  # type: ignore[attr-defined]
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertNotIn("wake-secret-do-not-log", "\n".join(self.logs.messages))

    def test_connection_failure_does_not_store_or_log_the_secret(self) -> None:
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = f"http://127.0.0.1:{port}/wake"
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = _AUTH
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "hello")

        self.assertEqual(result["error"], "error")
        self.assertEqual(result["detail"], "request failed")
        self.assertNotIn("note", result)
        self.assertNotIn("wake-secret-do-not-log", result["detail"])
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertTrue(self.logs.messages)
        joined = "\n".join(self.logs.messages)
        self.assertNotIn("wake-secret-do-not-log", joined)
        self.assertNotIn("hello", joined)

    def test_hungup_and_missing_session_do_not_deliver(self) -> None:
        webhook = self._webhook(204)
        self._use_webhook(webhook)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")
        self.store.hangup(session["session_id"], "local")

        with self.assertRaises(RuntimeError):
            dispatch_send(self.store, session["session_id"], "local", "hello")
        with self.assertRaises(KeyError):
            dispatch_send(self.store, "missing", "local", "hello")
        self.assertIsNone(webhook.captured)  # type: ignore[attr-defined]

    def _profiles(self, seats: dict[str, str]) -> None:
        root = Path(self.temp.name) / "agents"
        root.mkdir(exist_ok=True)
        for seat, name in seats.items():
            seat_dir = root / seat
            seat_dir.mkdir()
            (seat_dir / "profile.json").write_text(
                json.dumps({"name": name, "title": "役", "description": "説明"}),
                encoding="utf-8",
            )
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = str(root)

    def _use_webhook(self, webhook: ThreadingHTTPServer) -> None:
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(webhook) + "/wake"
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = _AUTH

    def _use_gateway(self, gateway: ThreadingHTTPServer) -> None:
        os.environ["GROKBOT_GATEWAY_URL"] = self._url(gateway)
        os.environ["GROKBOT_GATEWAY_TOKEN"] = _GATEWAY_TOKEN

    def _webhook(
        self,
        status: int,
        location: str | None = None,
    ) -> ThreadingHTTPServer:
        handler = type(
            f"Webhook{status}{id(self)}",
            (_WebhookHandler,),
            {"status_code": status, "location": location},
        )
        return self._serve(handler)

    def _gateway(self) -> ThreadingHTTPServer:
        return self._serve(_GatewayHandler)

    def _directory(self, payload: dict) -> ThreadingHTTPServer:
        handler = type(
            f"Directory{id(self)}{id(payload)}",
            (_DirectoryHandler,),
            {"payload": payload},
        )
        return self._serve(handler)

    def _serve(self, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.captured = None  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def _captured(self, server: ThreadingHTTPServer) -> dict:
        captured = server.captured  # type: ignore[attr-defined]
        self.assertIsNotNone(captured)
        return captured

    @staticmethod
    def _url(server: ThreadingHTTPServer) -> str:
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"


if __name__ == "__main__":
    unittest.main()
