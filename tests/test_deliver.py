"""Local call_send delivers through the Grok Bot gateway (mocked)."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from call_bridge.db import CallStore  # noqa: E402
from call_bridge.deliver import (  # noqa: E402
    DELIVER_TIMEOUT_SECONDS,
    dispatch_send,
    text_for_agent,
)

_ENV_KEYS = (
    "GROKBOT_GATEWAY_URL",
    "GROKBOT_GATEWAY_TOKEN",
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
_TOKEN = "gw-token-do-not-log"


class _GatewayHandler(BaseHTTPRequestHandler):
    status_code = 200
    payload: dict | None = None
    raw: bytes | None = None
    location: str | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.captured = {  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "origin": self.headers.get("Origin"),
            "header_names": [key.lower() for key in self.headers.keys()],
            "body": body,
        }
        if self.raw is not None:
            raw = self.raw
        elif self.payload is None:
            raw = b""
        else:
            raw = json.dumps(self.payload).encode("utf-8")
        self.send_response(self.status_code)
        if self.location:
            self.send_header("Location", self.location)
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
        logging.getLogger("call_bridge.deliver").addHandler(self.logs)
        logging.getLogger("call_bridge.deliver").setLevel(logging.DEBUG)

    def tearDown(self) -> None:
        logging.getLogger("call_bridge.deliver").removeHandler(self.logs)
        self.temp.cleanup()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_mcp_call_send_delegates_to_dispatch(self) -> None:
        from call_bridge import server

        sentinel = {"delivery": {"status": "delivered", "detail": "http 200"}}
        with mock.patch.object(server, "dispatch_send", return_value=sentinel) as dispatch:
            result = server.call_send("sess", "local", "hello", False)
        dispatch.assert_called_once_with(server.store, "sess", "local", "hello", False)
        self.assertEqual(result, sentinel)

    def test_timeout_is_short(self) -> None:
        self.assertGreaterEqual(DELIVER_TIMEOUT_SECONDS, 5)
        self.assertLessEqual(DELIVER_TIMEOUT_SECONDS, 10)

    def test_local_send_delivers_and_stores(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._use_gateway(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ", "check")

        result = dispatch_send(self.store, session["session_id"], "local", "状況を教えてください")

        self.assertNotIn("error", result)
        self.assertEqual(result["delivery"], {"status": "delivered", "detail": "http 200"})
        self.assertEqual(result["status"], "open")
        req = self._captured(gateway)
        self.assertEqual(req["path"], "/api/deliverAgentMessage")
        self.assertEqual(req["authorization"], f"Bearer {_TOKEN}")
        self.assertEqual(req["content_type"], "application/json")
        self.assertIsNone(req["origin"])
        self.assertNotIn("origin", req["header_names"])
        body = json.loads(req["body"].decode("utf-8"))
        uuid.UUID(body["messageId"])
        self.assertEqual(body["from"], {
            "id": f"call-bridge:{session['session_id']}",
            "name": "Cursor",
        })
        self.assertEqual(body["toAgentId"], "rapi-agent")
        self.assertIn(session["session_id"], body["text"])
        self.assertIn("call-bridge MCP の call_send", body["text"])
        self.assertIn("from_party=member", body["text"])
        self.assertEqual(body["text"], result["message"])
        self.assertEqual(body["text"], text_for_agent(
            session["session_id"], "状況を教えてください", True
        ))
        stored = self.store.poll_messages(session["session_id"], "member", mark_delivered=False)
        self.assertEqual([item["message"] for item in stored["messages"]], [body["text"]])
        self.assertNotIn(_TOKEN, "\n".join(self.logs.messages))

    def test_notice_is_delivered_without_a_reply_demand(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._use_gateway(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(
            self.store, session["session_id"], "local", "共有だけです", reply_required=False
        )

        self.assertEqual(result["message"], "共有だけです")
        body = json.loads(self._captured(gateway)["body"].decode("utf-8"))
        self.assertIn(f"session_id={session['session_id']}", body["text"])
        self.assertIn("返信不要", body["text"])
        self.assertNotIn("返信が必要です", body["text"])
        self.assertTrue(body["text"].startswith("共有だけです\n"))

    def test_member_send_does_not_call_the_gateway(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._use_gateway(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "member", "確認しました")

        self.assertEqual(result["message"], "確認しました")
        self.assertNotIn("delivery", result)
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]

    def test_missing_gateway_env_does_not_store(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "届かない")

        self.assertEqual(result["error"], "gateway_not_configured")
        self.assertIn("GROKBOT_GATEWAY_URL", result["detail"])
        self.assertIn("GROKBOT_GATEWAY_TOKEN", result["detail"])
        self.assertEqual(result["delivery"]["status"], "error")
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertEqual(self.store.get_session(session["session_id"])["status"], "ringing")
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]

    def test_url_without_token_does_not_call_gateway(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        os.environ["GROKBOT_GATEWAY_URL"] = self._url(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "届かない")

        self.assertEqual(result["error"], "gateway_not_configured")
        self.assertIn("GROKBOT_GATEWAY_TOKEN", result["detail"])
        self.assertNotIn("GROKBOT_GATEWAY_URL", result["detail"])
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]
        self.assertNotIn(_TOKEN, "\n".join(self.logs.messages))

    def test_bearer_prefix_in_token_is_not_doubled(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        os.environ["GROKBOT_GATEWAY_URL"] = self._url(gateway)
        os.environ["GROKBOT_GATEWAY_TOKEN"] = f"Bearer {_TOKEN}"
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "hello")

        self.assertEqual(result["delivery"]["status"], "delivered")
        self.assertEqual(self._captured(gateway)["authorization"], f"Bearer {_TOKEN}")

    def test_remote_directory_id_is_the_target(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._use_gateway(gateway)
        directory = self._directory({
            "ok": True,
            "source": "agent-profiles",
            "members": [{"name": "ラピ", "id": "  live-agent-id  ", "title": "インフラ統括"}],
        })
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(directory)
        self._profiles({"local-folder-must-not-win": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        dispatch_send(self.store, session["session_id"], "local", "hello")

        body = json.loads(self._captured(gateway)["body"].decode("utf-8"))
        self.assertEqual(body["toAgentId"], "live-agent-id")

    def test_remote_agent_id_field_and_id_fallback(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._use_gateway(gateway)
        directory = self._directory({
            "members": [{"name": "ラピ", "agentId": "from-agent-id"}],
        })
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(directory)
        by_name = self.store.open_session("local-1", "Cursor", "ラピ")
        dispatch_send(self.store, by_name["session_id"], "local", "hello")
        first = json.loads(self._captured(gateway)["body"].decode("utf-8"))
        self.assertEqual(first["toAgentId"], "from-agent-id")

        by_id = self.store.open_session("local-1", "", "from-agent-id")
        result = dispatch_send(self.store, by_id["session_id"], "local", "hello")
        self.assertEqual(result["delivery"]["status"], "delivered")
        second = json.loads(self._captured(gateway)["body"].decode("utf-8"))
        self.assertEqual(second["toAgentId"], "from-agent-id")
        self.assertEqual(second["from"]["name"], "local-1")

    def test_snapshot_without_id_does_not_store_or_call_gateway(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._use_gateway(gateway)
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
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]

    def test_unknown_member_and_ambiguous_id(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._use_gateway(gateway)
        self._profiles({"one": "ラピ"})
        missing_session = self.store.open_session("local-1", "Cursor", "いない人")

        missing = dispatch_send(self.store, missing_session["session_id"], "local", "hello")
        self.assertEqual(missing["error"], "target_not_found")
        self.assertIn("no directory entry", missing["detail"])
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]

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
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]

    def test_gateway_status_is_returned_and_not_stored(self) -> None:
        gateway = self._gateway({"status": "target_not_found"})
        self._use_gateway(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "hello")

        self.assertEqual(result["error"], "target_not_found")
        self.assertEqual(result["delivery"]["status"], "target_not_found")
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)

    def test_not_member_and_empty_pass_through(self) -> None:
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")
        for status in ("not_member", "unavailable", "empty"):
            gateway = self._gateway({"status": status})
            self._use_gateway(gateway)
            result = dispatch_send(self.store, session["session_id"], "local", "hello")
            self.assertEqual(result["error"], status)
            self.assertEqual(result["delivery"]["status"], status)
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)

    def test_http_401_and_400_do_not_store(self) -> None:
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")
        denied = self._gateway(None, status=401)
        self._use_gateway(denied)
        result = dispatch_send(self.store, session["session_id"], "local", "hello")
        self.assertEqual(result["error"], "error")
        self.assertEqual(result["detail"], "http 401")
        self.assertNotIn(_TOKEN, result["detail"])

        bad = self._gateway(
            {"message": "bad target", "failureCode": "invalid_target"},
            status=400,
        )
        self._use_gateway(bad)
        result = dispatch_send(self.store, session["session_id"], "local", "hello")
        self.assertEqual(result["error"], "error")
        self.assertIn("http 400", result["detail"])
        self.assertIn("invalid_target", result["detail"])
        self.assertIn("bad target", result["detail"])
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertNotIn(_TOKEN, "\n".join(self.logs.messages))

    def test_unknown_status_and_bad_json_are_errors(self) -> None:
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")
        weird = self._gateway({"status": "maybe"})
        self._use_gateway(weird)
        result = dispatch_send(self.store, session["session_id"], "local", "hello")
        self.assertEqual(result["error"], "error")
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)

        junk = self._gateway(None, status=200, raw=b"not-json")
        self._use_gateway(junk)
        result = dispatch_send(self.store, session["session_id"], "local", "hello")
        self.assertEqual(result["error"], "error")
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)

    def test_redirect_is_not_followed(self) -> None:
        bait = self._gateway({"status": "delivered"})
        gateway = self._gateway(None, status=302, location=self._url(bait))
        self._use_gateway(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "hello")

        self.assertEqual(result["error"], "error")
        self.assertEqual(result["detail"], "http 302")
        self.assertIsNone(bait.captured)  # type: ignore[attr-defined]
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertNotIn(_TOKEN, "\n".join(self.logs.messages))

    def test_connection_failure_does_not_store_or_log_the_token(self) -> None:
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        os.environ["GROKBOT_GATEWAY_URL"] = f"http://127.0.0.1:{port}"
        os.environ["GROKBOT_GATEWAY_TOKEN"] = _TOKEN
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(self.store, session["session_id"], "local", "hello")

        self.assertEqual(result["error"], "error")
        self.assertEqual(result["detail"], "request failed")
        self.assertNotIn(_TOKEN, result["detail"])
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)
        self.assertTrue(self.logs.messages)
        self.assertNotIn(_TOKEN, "\n".join(self.logs.messages))

    def test_hungup_and_missing_session_do_not_deliver(self) -> None:
        gateway = self._gateway({"status": "delivered"})
        self._use_gateway(gateway)
        self._profiles({"rapi-agent": "ラピ"})
        session = self.store.open_session("local-1", "Cursor", "ラピ")
        self.store.hangup(session["session_id"], "local")

        with self.assertRaises(RuntimeError):
            dispatch_send(self.store, session["session_id"], "local", "hello")
        with self.assertRaises(KeyError):
            dispatch_send(self.store, "missing", "local", "hello")
        self.assertIsNone(gateway.captured)  # type: ignore[attr-defined]

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

    def _use_gateway(self, gateway: ThreadingHTTPServer) -> None:
        os.environ["GROKBOT_GATEWAY_URL"] = self._url(gateway)
        os.environ["GROKBOT_GATEWAY_TOKEN"] = _TOKEN

    def _gateway(
        self,
        payload: dict | None,
        *,
        status: int = 200,
        location: str | None = None,
        raw: bytes | None = None,
    ) -> ThreadingHTTPServer:
        handler = type(
            f"Gateway{status}{id(self)}{id(payload)}",
            (_GatewayHandler,),
            {"status_code": status, "payload": payload, "location": location, "raw": raw},
        )
        return self._serve(handler)

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
