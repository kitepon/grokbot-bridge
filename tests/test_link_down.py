"""Wake the switchboard once when a real request finds a box link down."""

from __future__ import annotations

import json
import logging
import os
import socket
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

from call_bridge.db import CallStore  # noqa: E402
from call_bridge.deliver import dispatch_send  # noqa: E402
from call_bridge.directory import load_directory, search_directory  # noqa: E402
from call_bridge.wake import (  # noqa: E402
    LINK_DOWN_EVENT,
    LINK_DOWN_MIN_INTERVAL_SECONDS,
    LINK_DOWN_NOTE,
    build_link_down_payload,
    build_wake_payload,
    notify_link_down,
    notify_wake,
    reset_link_down_limiter,
)

_ENV_KEYS = (
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
    "GROKBOT_GATEWAY_URL",
    "GROKBOT_GATEWAY_TOKEN",
)

_MISSING_AGENTS = "/nonexistent/call-bridge-agents-root"
_MISSING_SNAPSHOT = "/nonexistent/call-bridge-directory.json"
_GATEWAY_TOKEN = "gw-token-do-not-log"
_WAKE_AUTH = "Bearer wake-secret-do-not-log"


class _CaptureHandler(BaseHTTPRequestHandler):
    status_code = 204
    location: str | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        self.send_response(self.status_code)
        if self.location:
            self.send_header("Location", self.location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _UnixHandler(BaseHTTPRequestHandler):
    status_code = 200
    payload = b"{}"

    def do_GET(self) -> None:  # noqa: N802
        body = self.payload
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _session() -> dict:
    return {
        "session_id": "sess-1",
        "status": "ringing",
        "member_name": "ラピ",
        "local_id": "local-1",
        "local_label": "cursor",
        "purpose": "check the deploy",
        "created_at": "2026-09-24T00:00:00+00:00",
        "message": "must-not-leak",
    }


class LinkDownTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = _MISSING_AGENTS
        os.environ["CALL_BRIDGE_DIRECTORY"] = _MISSING_SNAPSHOT
        reset_link_down_limiter()
        self.temp = tempfile.TemporaryDirectory()
        self.logs = _ListHandler()
        logging.getLogger("call_bridge.wake").addHandler(self.logs)
        logging.getLogger("call_bridge.wake").setLevel(logging.DEBUG)

    def tearDown(self) -> None:
        logging.getLogger("call_bridge.wake").removeHandler(self.logs)
        self.temp.cleanup()
        reset_link_down_limiter()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_interval_is_one_minute_and_payload_is_the_three_fields(self) -> None:
        self.assertEqual(LINK_DOWN_MIN_INTERVAL_SECONDS, 60)
        payload = build_link_down_payload("directory", "unix socket not found")
        self.assertEqual(
            payload,
            {
                "event": "bridge.link_down",
                "link": "directory",
                "detail": "unix socket not found",
            },
        )
        self.assertEqual(set(payload), {"event", "link", "detail"})
        self.assertEqual(payload["event"], LINK_DOWN_EVENT)
        self.assertNotIn("must-not-leak", json.dumps(build_wake_payload(_session())))

    def test_payload_scrubs_gateway_and_webhook_secrets(self) -> None:
        os.environ["GROKBOT_GATEWAY_TOKEN"] = _GATEWAY_TOKEN
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = _WAKE_AUTH
        payload = build_link_down_payload(
            "gateway",
            f"request failed {_GATEWAY_TOKEN} {_WAKE_AUTH}",
        )
        encoded = json.dumps(payload)
        self.assertNotIn(_GATEWAY_TOKEN, encoded)
        self.assertNotIn("wake-secret-do-not-log", encoded)
        self.assertIn("***", payload["detail"])

    def test_post_uses_the_session_wake_url_and_auth(self) -> None:
        server = self._webhook(204)
        alias = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL"] = self._url(alias)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = _WAKE_AUTH
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH"] = "Bearer alias-must-not-win"

        result = notify_link_down("gateway", "unavailable")

        self.assertEqual(result, {"status": "ok", "detail": "http 204"})
        self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]
        self.assertEqual(alias.requests, [])  # type: ignore[attr-defined]
        req = server.requests[0]  # type: ignore[attr-defined]
        self.assertEqual(req["authorization"], _WAKE_AUTH)
        self.assertEqual(req["content_type"], "application/json")
        self.assertEqual(
            json.loads(req["body"].decode("utf-8")),
            {"event": "bridge.link_down", "link": "gateway", "detail": "unavailable"},
        )
        self.assertNotIn(_WAKE_AUTH, "\n".join(self.logs.messages))
        self.assertNotIn("wake-secret-do-not-log", "\n".join(self.logs.messages))

    def test_alias_url_when_primary_unset(self) -> None:
        server = self._webhook(204)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_SWITCHBOARD_WEBHOOK_AUTH"] = _WAKE_AUTH

        result = notify_link_down("directory", "timed out")

        self.assertEqual(result["status"], "ok")
        req = server.requests[0]  # type: ignore[attr-defined]
        self.assertEqual(req["authorization"], _WAKE_AUTH)
        body = json.loads(req["body"].decode("utf-8"))
        self.assertEqual(body["link"], "directory")
        self.assertEqual(body["detail"], "timed out")

    def test_unset_url_does_not_start_the_interval(self) -> None:
        server = self._webhook(204)
        clock = {"now": 1000.0}
        with mock.patch("call_bridge.wake.time.monotonic", side_effect=lambda: clock["now"]):
            skipped = notify_link_down("directory", "request failed")
            os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
            posted = notify_link_down("directory", "request failed")

        self.assertEqual(skipped, {"status": "skipped", "detail": "webhook url unset"})
        self.assertEqual(posted["status"], "ok")
        self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]

    def test_at_most_one_wake_per_minute_across_both_links(self) -> None:
        server = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        clock = {"now": 1000.0}

        def now() -> float:
            return clock["now"]

        with mock.patch("call_bridge.wake.time.monotonic", side_effect=now):
            first = notify_link_down("directory", "unix socket not found")
            clock["now"] = 1059.9
            second = notify_link_down("gateway", "timed out")
            clock["now"] = 1060.0
            third = notify_link_down("gateway", "unavailable")

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second, {"status": "skipped", "detail": "rate limited"})
        self.assertEqual(third["status"], "ok")
        bodies = [json.loads(item["body"].decode("utf-8")) for item in server.requests]  # type: ignore[attr-defined]
        self.assertEqual(
            bodies,
            [
                {"event": "bridge.link_down", "link": "directory", "detail": "unix socket not found"},
                {"event": "bridge.link_down", "link": "gateway", "detail": "unavailable"},
            ],
        )

    def test_session_opened_wake_is_not_rate_limited(self) -> None:
        server = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = _WAKE_AUTH
        clock = {"now": 5000.0}
        with mock.patch("call_bridge.wake.time.monotonic", side_effect=lambda: clock["now"]):
            down = notify_link_down("gateway", "request failed")
            opened = notify_wake(_session())
            again = notify_wake(_session())
            limited = notify_link_down("directory", "timed out")

        self.assertEqual(down["status"], "ok")
        self.assertEqual(opened["status"], "ok")
        self.assertEqual(again["status"], "ok")
        self.assertEqual(limited["detail"], "rate limited")
        events = [
            json.loads(item["body"].decode("utf-8"))["event"]
            for item in server.requests  # type: ignore[attr-defined]
        ]
        self.assertEqual(events, ["bridge.link_down", "session.opened", "session.opened"])
        opened_body = json.loads(server.requests[1]["body"].decode("utf-8"))  # type: ignore[attr-defined]
        self.assertEqual(opened_body["event"], "session.opened")
        self.assertNotIn("link", opened_body)
        self.assertEqual(server.requests[1]["authorization"], _WAKE_AUTH)  # type: ignore[attr-defined]

    def test_redirect_is_not_followed_and_still_counts_toward_the_interval(self) -> None:
        bait = self._webhook(204)
        server = self._webhook(302, location=self._url(bait))
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = _WAKE_AUTH
        clock = {"now": 1000.0}
        with mock.patch("call_bridge.wake.time.monotonic", side_effect=lambda: clock["now"]):
            result = notify_link_down("directory", "request failed")
            clock["now"] = 1010.0
            limited = notify_link_down("directory", "request failed")

        self.assertEqual(result, {"status": "error", "detail": "http 302"})
        self.assertEqual(limited["detail"], "rate limited")
        self.assertEqual(bait.requests, [])  # type: ignore[attr-defined]
        self.assertNotIn("wake-secret-do-not-log", "\n".join(self.logs.messages))

    def test_missing_unix_socket_keeps_fallback_and_wakes_once(self) -> None:
        webhook = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(webhook)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = _WAKE_AUTH
        os.environ["CALL_BRIDGE_DIRECTORY_UNIX"] = str(Path(self.temp.name) / "missing.sock")
        root = Path(self.temp.name) / "agents"
        _write_profile(root, "rapi", "ラピ")
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = str(root)

        book = search_directory("ラピ")
        again = load_directory()

        self.assertTrue(book["ok"])
        self.assertEqual(book["source"], "agent-profiles")
        self.assertEqual(book["count"], 1)
        self.assertEqual(book["members"][0]["name"], "ラピ")
        self.assertEqual(book["note"], LINK_DOWN_NOTE)
        self.assertEqual(again["note"], LINK_DOWN_NOTE)
        self.assertEqual(len(webhook.requests), 1)  # type: ignore[attr-defined]
        body = json.loads(webhook.requests[0]["body"].decode("utf-8"))  # type: ignore[attr-defined]
        self.assertEqual(body["link"], "directory")
        self.assertEqual(body["detail"], "unix socket not found")
        self.assertNotIn("wake-secret-do-not-log", json.dumps(body))

    def test_unix_http_error_and_bad_json_do_not_wake(self) -> None:
        webhook = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(webhook)
        path = self._snapshot([{"name": "スナップ", "title": "古い", "role": "予備"}])
        sock = self._unix(status=502, payload=b"{}")
        os.environ["CALL_BRIDGE_DIRECTORY_UNIX"] = sock

        http_error = load_directory()
        self._unix_servers[sock].RequestHandlerClass.status_code = 200
        self._unix_servers[sock].RequestHandlerClass.payload = b"not-json"
        bad_json = load_directory()

        self.assertEqual(http_error["source"], "directory.json")
        self.assertEqual(http_error["directory_unix_error"], "http 502")
        self.assertEqual(http_error["path"], str(path.resolve()))
        self.assertNotIn("note", http_error)
        self.assertEqual(bad_json["directory_unix_error"], "invalid directory json")
        self.assertNotIn("note", bad_json)
        self.assertEqual(webhook.requests, [])  # type: ignore[attr-defined]

    def test_url_only_failure_does_not_wake(self) -> None:
        webhook = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(webhook)
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._closed_http()
        self._snapshot([{"name": "スナップ", "role": "予備"}])

        book = load_directory()

        self.assertEqual(book["source"], "directory.json")
        self.assertEqual(book["directory_url_error"], "request failed")
        self.assertNotIn("note", book)
        self.assertEqual(webhook.requests, [])  # type: ignore[attr-defined]

    def test_closed_gateway_is_not_used_and_does_not_emit_link_down(self) -> None:
        webhook = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(webhook)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_AUTH"] = _WAKE_AUTH
        os.environ["GROKBOT_GATEWAY_URL"] = self._closed_origin()
        os.environ["GROKBOT_GATEWAY_TOKEN"] = _GATEWAY_TOKEN
        self._profiles({"rapi-agent": "ラピ"})
        store = CallStore(Path(self.temp.name) / "calls.sqlite")
        session = store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(store, session["session_id"], "local", "hello")

        self.assertEqual(result["delivery"]["status"], "delivered")
        self.assertNotIn("note", result)
        self.assertEqual(store.session_info(session["session_id"])["message_count"], 1)
        self.assertEqual(len(webhook.requests), 1)  # type: ignore[attr-defined]
        body = json.loads(webhook.requests[0]["body"].decode("utf-8"))  # type: ignore[attr-defined]
        self.assertEqual(body["event"], "session.message")
        self.assertEqual(body["message"], "hello")
        self.assertEqual(body["member_agent_id"], "rapi-agent")
        posted = webhook.requests[0]["body"].decode("utf-8")  # type: ignore[attr-defined]
        self.assertNotIn(_GATEWAY_TOKEN, posted)
        self.assertNotIn("wake-secret-do-not-log", posted)
        self.assertNotIn(_GATEWAY_TOKEN, "\n".join(self.logs.messages))

    def test_message_webhook_failure_does_not_emit_link_down(self) -> None:
        webhook = self._webhook(502)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(webhook)
        self._profiles({"rapi-agent": "ラピ"})
        store = CallStore(Path(self.temp.name) / "calls.sqlite")
        session = store.open_session("local-1", "Cursor", "ラピ")

        with mock.patch("call_bridge.wake.notify_link_down") as down:
            result = dispatch_send(store, session["session_id"], "local", "hello")

        down.assert_not_called()
        self.assertEqual(result["error"], "error")
        self.assertEqual(result["detail"], "http 502")
        self.assertNotIn("note", result)
        self.assertEqual(store.session_info(session["session_id"])["message_count"], 0)
        self.assertEqual(len(webhook.requests), 1)  # type: ignore[attr-defined]
        body = json.loads(webhook.requests[0]["body"].decode("utf-8"))  # type: ignore[attr-defined]
        self.assertEqual(body["event"], "session.message")

    def test_member_send_and_unset_webhook_do_not_emit_link_down(self) -> None:
        webhook = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(webhook)
        self._profiles({"rapi-agent": "ラピ"})
        store = CallStore(Path(self.temp.name) / "calls.sqlite")
        session = store.open_session("local-1", "Cursor", "ラピ")

        member = dispatch_send(store, session["session_id"], "member", "確認しました")
        os.environ.pop("CALL_BRIDGE_WAKE_WEBHOOK_URL", None)
        missing = dispatch_send(store, session["session_id"], "local", "hello")

        self.assertEqual(member["message"], "確認しました")
        self.assertNotIn("note", member)
        self.assertEqual(missing["error"], "webhook_not_configured")
        self.assertNotIn("note", missing)
        self.assertEqual(webhook.requests, [])  # type: ignore[attr-defined]
        self.assertEqual(store.session_info(session["session_id"])["message_count"], 1)

    def test_directory_down_during_send_uses_fallback_and_one_wake(self) -> None:
        webhook = self._webhook(204)
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = self._url(webhook)
        os.environ["CALL_BRIDGE_DIRECTORY_UNIX"] = str(Path(self.temp.name) / "missing.sock")
        self._profiles({"rapi-agent": "ラピ"})
        store = CallStore(Path(self.temp.name) / "calls.sqlite")
        session = store.open_session("local-1", "Cursor", "ラピ")

        result = dispatch_send(store, session["session_id"], "local", "hello")

        self.assertEqual(result["delivery"]["status"], "delivered")
        self.assertNotIn("note", result)
        self.assertEqual(store.session_info(session["session_id"])["message_count"], 1)
        self.assertEqual(len(webhook.requests), 2)  # type: ignore[attr-defined]
        first = json.loads(webhook.requests[0]["body"].decode("utf-8"))  # type: ignore[attr-defined]
        second = json.loads(webhook.requests[1]["body"].decode("utf-8"))  # type: ignore[attr-defined]
        self.assertEqual(first["event"], "bridge.link_down")
        self.assertEqual(first["link"], "directory")
        self.assertEqual(first["detail"], "unix socket not found")
        self.assertEqual(second["event"], "session.message")
        self.assertEqual(second["message"], "hello")
        self.assertEqual(second["member_agent_id"], "rapi-agent")

    def _profiles(self, seats: dict[str, str]) -> None:
        root = Path(self.temp.name) / "agents"
        root.mkdir(exist_ok=True)
        for seat, name in seats.items():
            _write_profile(root, seat, name)
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = str(root)

    def _snapshot(self, members: list[dict[str, str]]) -> Path:
        path = Path(self.temp.name) / "directory.json"
        path.write_text(
            json.dumps({"schema": "grokbot.directory.v0", "members": members}),
            encoding="utf-8",
        )
        os.environ["CALL_BRIDGE_DIRECTORY"] = str(path)
        return path

    def _webhook(self, status: int, location: str | None = None) -> ThreadingHTTPServer:
        handler = type(
            f"Wake{status}{id(self)}{location}",
            (_CaptureHandler,),
            {"status_code": status, "location": location},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.requests = []  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def _unix(self, *, status: int, payload: bytes) -> str:
        handler = type(
            f"Unix{status}{id(self)}",
            (_UnixHandler,),
            {"status_code": status, "payload": payload},
        )

        class UnixHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_UNIX

        sock_path = str(Path(self.temp.name) / f"d{status}.sock")
        server = UnixHTTPServer(sock_path, handler)
        self._unix_servers = getattr(self, "_unix_servers", {})
        self._unix_servers[sock_path] = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _stop() -> None:
            server.shutdown()
            server.server_close()
            if os.path.exists(sock_path):
                os.unlink(sock_path)

        self.addCleanup(_stop)
        return sock_path

    @staticmethod
    def _url(server: ThreadingHTTPServer) -> str:
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/wake"

    @staticmethod
    def _closed_origin() -> str:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return f"http://127.0.0.1:{port}"

    def _closed_http(self) -> str:
        return self._closed_origin() + "/v0/directory"


def _write_profile(root: Path, seat: str, name: str) -> None:
    seat_dir = root / seat
    seat_dir.mkdir(parents=True, exist_ok=True)
    (seat_dir / "profile.json").write_text(
        json.dumps({"name": name, "title": "役", "description": "説明"}, ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
