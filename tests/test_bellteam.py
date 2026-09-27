"""BellTeam direct routing does not depend on a GrokBot AI response."""

from __future__ import annotations

import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from call_bridge.bellteam import BellTeamError, BellTeamOutcomeUnknown, fetch_directory, send_delivery
from call_bridge.db import CallStore
from call_bridge.deliver import dispatch_send
from call_bridge.directory import load_directory, resolve_bellteam_member
from call_bridge import server as call_server


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = {"members": [{"id": "bot-a", "name": "トロニー", "role": "電子工学"}]}
        self._answer(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received = json.loads(self.rfile.read(length))
        self._answer({"delivery": "running", "delivery_id": "delivery-1"})

    def _answer(self, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


class UnixServer(socketserver.UnixStreamServer):
    allow_reuse_address = True


class SlowHandler(Handler):
    def do_POST(self):  # noqa: N802
        time.sleep(3.2)
        super().do_POST()


class BellTeamTests(unittest.TestCase):
    def test_unix_directory_and_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "bellteam.sock")
            server = UnixServer(path, Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.dict(os.environ, {"CALL_BRIDGE_BELLTEAM_UNIX": path}):
                    members = fetch_directory()
                    self.assertEqual(members[0]["system"], "bellteam")
                    receipt = send_delivery({"schema": "call-bridge.delivery.v1", "event": "session.message"})
                    self.assertEqual(receipt["delivery"], "running")
                    self.assertEqual(server.received["event"], "session.message")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_delivery_waits_for_acceptance_beyond_old_three_second_limit(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "bellteam.sock")
            server = UnixServer(path, SlowHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with mock.patch.dict(os.environ, {"CALL_BRIDGE_BELLTEAM_UNIX": path}):
                    receipt = send_delivery({"schema": "call-bridge.delivery.v1", "event": "session.message"})
                self.assertEqual(receipt["delivery"], "running")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_timeout_after_connect_is_unknown_and_connect_failure_is_definite(self):
        with mock.patch.dict(os.environ, {"CALL_BRIDGE_BELLTEAM_UNIX": "/socket"}), \
             mock.patch("call_bridge.bellteam._Connection") as connection:
            connection.return_value.getresponse.side_effect = TimeoutError()
            with self.assertRaises(BellTeamOutcomeUnknown):
                send_delivery({"event": "session.message"})
            connection.assert_called_with("/socket", timeout=30)
            connection.return_value.connect.side_effect = FileNotFoundError()
            with self.assertRaises(BellTeamError) as error:
                send_delivery({"event": "session.message"})
            self.assertNotIsInstance(error.exception, BellTeamOutcomeUnknown)

    def test_server_error_after_delivery_request_is_unknown(self):
        with mock.patch.dict(os.environ, {"CALL_BRIDGE_BELLTEAM_UNIX": "/socket"}), \
             mock.patch("call_bridge.bellteam._Connection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 502
            response.read.return_value = b'{"error":"delivery failed"}'
            with self.assertRaises(BellTeamOutcomeUnknown):
                send_delivery({"event": "session.message"})

    def test_combined_directory_survives_grok_failure_and_resolves_exact_id(self):
        with mock.patch("call_bridge.directory._load_grok_directory", return_value={"ok": False, "detail": "Grok offline"}), \
             mock.patch("call_bridge.directory.bellteam_socket_path", return_value="/socket"), \
             mock.patch("call_bridge.directory.fetch_bellteam_directory", return_value=[
                 {"system": "bellteam", "id": "bot-a", "name": "同名"},
                 {"system": "bellteam", "id": "bot-b", "name": "同名"},
             ]):
            directory = load_directory()
            self.assertTrue(directory["ok"])
            self.assertEqual(directory["count"], 2)
            self.assertEqual(resolve_bellteam_member("bot-a")["id"], "bot-a")
            self.assertEqual(resolve_bellteam_member("同名")["error"], "target_not_found")

    def test_bellteam_resolution_does_not_read_grok_directory(self):
        with mock.patch("call_bridge.directory._load_grok_directory") as grok, \
             mock.patch("call_bridge.directory.bellteam_socket_path", return_value="/socket"), \
             mock.patch("call_bridge.directory.fetch_bellteam_directory", return_value=[
                 {"system": "bellteam", "id": "bot-a", "name": "トロニー"},
             ]):
            self.assertEqual(resolve_bellteam_member("トロニー")["id"], "bot-a")
            grok.assert_not_called()

    def test_bellteam_target_acceptance_controls_storage_and_reply_callback(self):
        with tempfile.TemporaryDirectory() as root:
            store = CallStore(Path(root) / "calls.db")
            session = store.open_session("bot-caller", "エレグ", "トロニー", member_system="bellteam", member_id="bot-a", local_system="bellteam")
            sid = session["session_id"]
            with mock.patch("call_bridge.deliver.send_delivery", return_value={"delivery": "running"}) as send:
                sent = dispatch_send(store, sid, "local", "確認して")
                self.assertEqual(sent["delivery"]["status"], "delivered")
                self.assertEqual(send.call_args.args[0]["target_id"], "bot-a")
                self.assertEqual(send.call_args.args[0]["source_system"], "bellteam")
                reply = dispatch_send(store, sid, "member", "確認したよ")
                self.assertEqual(reply["delivery"]["status"], "delivered")
                self.assertEqual(send.call_args.args[0]["target_id"], "bot-caller")
                self.assertEqual(send.call_args.args[0]["event"], "session.reply")
            with mock.patch("call_bridge.deliver.send_delivery", side_effect=BellTeamError("BellTeam unavailable")):
                failed = dispatch_send(store, sid, "local", "届かない")
                self.assertEqual(failed["delivery"]["status"], "error")
                self.assertEqual(len(store.poll_messages(sid, "member")["messages"]), 1)
            with mock.patch("call_bridge.deliver.send_delivery", side_effect=BellTeamOutcomeUnknown("receipt timed out")) as send:
                unknown = dispatch_send(store, sid, "local", "届いたか不明")
                self.assertEqual(unknown["delivery"]["status"], "unknown")
                self.assertEqual(unknown["seq"], 3)
                send.assert_called_once()
                self.assertEqual(len(store.poll_messages(sid, "member")["messages"]), 2)
                reply = dispatch_send(store, sid, "member", "返信の配送も不明")
                self.assertEqual(reply["delivery"]["status"], "unknown")
                self.assertEqual(len(store.poll_messages(sid, "local")["messages"]), 2)

    def test_bellteam_call_open_does_not_wake_marian(self):
        with tempfile.TemporaryDirectory() as root:
            store = CallStore(Path(root) / "calls.db")
            with mock.patch.object(call_server, "store", store), \
                 mock.patch.object(call_server, "resolve_bellteam_member", return_value={"ok": True, "id": "bot-a", "name": "トロニー"}), \
                 mock.patch.object(call_server, "notify_wake") as wake:
                result = call_server.call_open("grok-caller", "ラピ", "bot-a", member_system="bellteam", local_system="grokbot")
                self.assertEqual(result["member_id"], "bot-a")
                self.assertEqual(result["wake"]["status"], "skipped")
                wake.assert_not_called()

    def test_grok_call_open_keeps_resolved_id_and_name(self):
        with tempfile.TemporaryDirectory() as root:
            store = CallStore(Path(root) / "calls.db")
            with mock.patch.object(call_server, "store", store), \
                 mock.patch.object(call_server, "resolve_member_agent_id", return_value={"ok": True, "id": "grok-a", "name": "ラピ"}), \
                 mock.patch.object(call_server, "notify_wake", return_value={"status": "skipped"}):
                result = call_server.call_open("bot-caller", "トロニー", "grok-a", local_system="bellteam")
                self.assertEqual(result["member_id"], "grok-a")
                self.assertEqual(result["member_name"], "ラピ")
                self.assertEqual(store.get_session(result["session_id"])["local_system"], "bellteam")


if __name__ == "__main__":
    unittest.main()
