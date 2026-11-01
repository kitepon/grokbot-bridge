"""The box courier delivers each GrokBot message once, through sendPrompt."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from call_bridge.auth import Principal  # noqa: E402
from call_bridge.box_courier import (  # noqa: E402
    BridgeClient,
    CourierBusy,
    CourierLock,
    Journal,
    format_pass,
    main as courier_main,
    pass_exit_code,
    restart_action,
    run_once,
)
from call_bridge.db import CallStore, delivery_nonce  # noqa: E402
from call_bridge.deliver import dispatch_send  # noqa: E402
from call_bridge.gateway import GatewayConfigError, load_gateway, send_prompt  # noqa: E402
from call_bridge import server  # noqa: E402
from call_bridge.sender import with_sender_preface  # noqa: E402


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class GatewayConfigTests(unittest.TestCase):
    def test_invalid_json_does_not_include_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gateway.json"
            path.write_text('{"token": "super-secret-token", ', encoding="utf-8")
            with self.assertRaises(GatewayConfigError) as caught:
                load_gateway(path)
        self.assertNotIn("super-secret-token", str(caught.exception))

    def test_port_and_token_become_a_loopback_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gateway.json"
            path.write_text(
                json.dumps({"port": 1340, "host": "0.0.0.0", "token": "gw-secret"}),
                encoding="utf-8",
            )
            loaded = load_gateway(path)
        self.assertEqual(loaded["base_url"], "http://127.0.0.1:1340")
        self.assertEqual(loaded["token"], "gw-secret")


class SendPromptTests(unittest.TestCase):
    def test_body_shape_and_the_token_stays_out_of_the_log(self) -> None:
        captured: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                captured["path"] = self.path
                captured["authorization"] = self.headers.get("Authorization")
                captured["body"] = json.loads(self.rfile.read(length))
                raw = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        logs = _ListHandler()
        logging.getLogger("call_bridge.gateway").addHandler(logs)
        self.addCleanup(logging.getLogger("call_bridge.gateway").removeHandler, logs)
        port = httpd.server_address[1]
        prompt = with_sender_preface("本文", "ローカル", "Cursor")
        result = send_prompt(f"http://127.0.0.1:{port}", "gw-secret", "rapi-agent", "nonce-1", prompt)
        self.assertEqual(result["outcome"], "accepted")
        self.assertEqual(captured["path"], "/api/sendPrompt")
        self.assertEqual(captured["authorization"], "Bearer gw-secret")
        self.assertEqual(captured["body"], {
            "agentId": "rapi-agent",
            "clientNonce": "nonce-1",
            "prompt": prompt,
            "automationWriteProvenance": "untrusted",
        })
        self.assertNotIn("gw-secret", "\n".join(logs.messages))
        self.assertNotIn("本文", "\n".join(logs.messages))

    def test_connection_refused_is_rejected_and_a_closed_read_is_not(self) -> None:
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        refused = send_prompt(f"http://127.0.0.1:{port}", "gw-secret", "rapi", "n", "本文", timeout=1)
        self.assertEqual(refused["outcome"], "rejected")
        self.assertNotIn("gw-secret", refused["detail"])


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CallStore(Path(self.tmp.name) / "calls.db")
        agents = Path(self.tmp.name) / "agents" / "rapi-agent"
        agents.mkdir(parents=True)
        (agents / "profile.json").write_text('{"name":"ラピ"}', encoding="utf-8")
        self._saved = {key: os.environ.get(key) for key in (
            "CALL_BRIDGE_AGENTS_ROOT", "CALL_BRIDGE_DIRECTORY", "CALL_BRIDGE_DIRECTORY_UNIX",
            "CALL_BRIDGE_DIRECTORY_URL", "CALL_BRIDGE_DIRECTORY_PUSH", "CALL_BRIDGE_GROKBOT_DELIVERY",
            "CALL_BRIDGE_WAKE_WEBHOOK_URL",
        )}
        for key in self._saved:
            os.environ.pop(key, None)
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = str(agents.parent)
        os.environ["CALL_BRIDGE_DIRECTORY"] = str(Path(self.tmp.name) / "no.json")
        os.environ["CALL_BRIDGE_DIRECTORY_PUSH"] = str(Path(self.tmp.name) / "no-push.json")
        os.environ["CALL_BRIDGE_GROKBOT_DELIVERY"] = "courier"
        os.environ["CALL_BRIDGE_WAKE_WEBHOOK_URL"] = "http://127.0.0.1:9/wake"

    def tearDown(self) -> None:
        self.tmp.cleanup()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _send(self, text: str = "本文") -> dict:
        session = self.store.open_session("local-1", "Cursor", "ラピ", member_id="rapi-agent")
        with mock.patch("call_bridge.deliver.notify_ring") as ring:
            result = dispatch_send(
                self.store, session["session_id"], "local", text,
                sender_name="Cursor", sender_team="ローカル",
            )
        ring.assert_not_called()
        self.assertEqual(result["delivery"]["status"], "queued", result)
        self.assertEqual(result["message"].count("これはオーナー本人ではなく、"), 1)
        return result

    def test_courier_mode_queues_without_marian_and_a_second_claim_is_empty(self) -> None:
        sent = self._send()
        first = self.store.claim_grokbot_deliveries("box", 5)
        second = self.store.claim_grokbot_deliveries("other", 5)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(first[0]["agent_id"], "rapi-agent")
        self.assertEqual(first[0]["nonce"], delivery_nonce(sent["session_id"], sent["seq"]))
        self.assertTrue(first[0]["prompt"].startswith(
            "これはオーナー本人ではなく、ローカルチームのCursorからのメッセージです。"
        ))
        info = self.store.session_info(sent["session_id"])
        self.assertEqual(info["grokbot_delivery"][0]["state"], "inflight")
        self.assertNotIn("claim_token", info["grokbot_delivery"][0])

    def test_unknown_is_not_claimed_again_and_a_definite_failure_retries_the_same_nonce(self) -> None:
        sent = self._send()
        claimed = self.store.claim_grokbot_deliveries("box")[0]
        done = self.store.finish_grokbot_delivery(
            sent["session_id"], sent["seq"], claimed["claim_token"], "unknown", "timed out", True
        )
        self.assertEqual(done["state"], "unknown")
        self.assertEqual(self.store.claim_grokbot_deliveries("box"), [])

        again = self._send("もう一通")
        claim = self.store.claim_grokbot_deliveries("box")[0]
        nonce = claim["nonce"]
        self.assertEqual(nonce, delivery_nonce(again["session_id"], again["seq"]))
        for _ in range(2):
            finished = self.store.finish_grokbot_delivery(
                again["session_id"], again["seq"], claim["claim_token"], "failed", "http 400", True
            )
            self.assertEqual(finished["state"], "queued")
            claim = self.store.claim_grokbot_deliveries("box")[0]
            self.assertEqual(claim["nonce"], nonce)
        stopped = self.store.finish_grokbot_delivery(
            again["session_id"], again["seq"], claim["claim_token"], "failed", "http 400", True
        )
        self.assertEqual(stopped["state"], "failed")
        self.assertEqual(self.store.claim_grokbot_deliveries("box"), [])

    def test_duplicate_submitted_receipt_is_idempotent(self) -> None:
        sent = self._send()
        claim = self.store.claim_grokbot_deliveries("box")[0]
        first = self.store.finish_grokbot_delivery(
            sent["session_id"], sent["seq"], claim["claim_token"], "submitted", "http 200"
        )
        second = self.store.finish_grokbot_delivery(
            sent["session_id"], sent["seq"], claim["claim_token"], "submitted", "http 200"
        )
        self.assertEqual(first["state"], second["state"])
        self.assertEqual(self.store.claim_grokbot_deliveries("box"), [])

    def test_hungup_drops_a_queued_prompt(self) -> None:
        sent = self._send()
        self.store.hangup(sent["session_id"], "local")
        self.assertEqual(self.store.claim_grokbot_deliveries("box"), [])
        self.assertEqual(self.store.grokbot_deliveries(sent["session_id"])[0]["state"], "failed")

    def test_only_the_grokbot_side_can_report_sendprompt(self) -> None:
        sent = self._send()
        claim = self.store.claim_grokbot_deliveries("box")[0]
        with mock.patch.object(server, "store", self.store):
            local = Principal(name="mac", system="local")
            denied = server._receipt(local, sent["session_id"], sent["seq"], "submitted", None, None, claim["claim_token"])
            member = Principal(name="grokbot", system="grokbot")
            not_courier = server._receipt(
                member, sent["session_id"], sent["seq"], "submitted", "http 200", None, claim["claim_token"]
            )
            grok = Principal(name="grokbot-courier", system="grokbot", courier=True)
            reported = server._receipt(
                grok, sent["session_id"], sent["seq"], "submitted", "http 200", None, claim["claim_token"]
            )
        self.assertEqual(denied["error"], "forbidden")
        self.assertEqual(not_courier["error"], "forbidden")
        self.assertEqual(reported["state"], "submitted")


class CourierLoopTests(unittest.TestCase):
    def test_restart_actions(self) -> None:
        self.assertEqual(restart_action(None), "send")
        self.assertEqual(restart_action("held"), "send")
        self.assertEqual(restart_action("armed"), "unknown")
        self.assertEqual(restart_action("uncertain"), "unknown")
        self.assertEqual(restart_action("accepted"), "submitted")
        self.assertEqual(restart_action("rejected"), "failed")

    def test_a_delivered_prompt_is_not_sent_twice_and_an_armed_journal_is_not_resent(self) -> None:
        world = _World()
        self.addCleanup(world.close)
        prompt = with_sender_preface("本文", "ローカル", "Cursor")
        world.queue(prompt, "rapi-agent", "nonce-1")
        journal = Journal(world.state / "journal.json")
        gateway = {"base_url": world.gateway_url, "token": "gw-secret"}
        bridge = BridgeClient(world.bridge_url, "bridge-secret")

        first = run_once(bridge, journal, gateway, "box")
        self.assertEqual(first["sent"], 1)
        self.assertEqual(len(world.prompts), 1)
        self.assertEqual(world.prompts[0]["body"]["clientNonce"], "nonce-1")
        self.assertEqual(world.prompts[0]["body"]["automationWriteProvenance"], "untrusted")
        self.assertEqual(world.prompts[0]["authorization"], "Bearer gw-secret")
        self.assertNotIn("gw-secret", json.dumps(world.receipts))
        self.assertEqual(world.receipts[-1]["state"], "submitted")

        second = run_once(bridge, journal, gateway, "box")
        self.assertEqual(len(world.prompts), 1, world.prompts)
        self.assertEqual(second["sent"], 0)

        world.queue(prompt, "rapi-agent", "nonce-2")
        claimed = world.claim_for_test("box")
        journal.put(claimed["session_id"], claimed["seq"], "armed", nonce="nonce-2", claim_token=claimed["claim_token"])
        run_once(bridge, journal, gateway, "box")
        self.assertEqual([item["body"]["clientNonce"] for item in world.prompts], ["nonce-1"])
        self.assertEqual(world.receipts[-1]["state"], "unknown")

        # Two claims already in flight. Finishing the first must not forget the
        # second journal entry and send that prompt again.
        world.queue(prompt, "rapi-agent", "nonce-3")
        world.queue(prompt, "rapi-agent", "nonce-4")
        first_held = world.claim_for_test("box")
        second_held = world.claim_for_test("box")
        journal.put(first_held["session_id"], first_held["seq"], "armed", nonce="nonce-3", claim_token=first_held["claim_token"])
        journal.put(second_held["session_id"], second_held["seq"], "armed", nonce="nonce-4", claim_token=second_held["claim_token"])
        run_once(bridge, journal, gateway, "box")
        self.assertEqual([item["body"]["clientNonce"] for item in world.prompts], ["nonce-1"])
        self.assertEqual([item["state"] for item in world.receipts[-2:]], ["unknown", "unknown"])

    def test_second_process_does_not_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["CALL_BRIDGE_COURIER_STATE"] = tmp
            env["PYTHONPATH"] = str(SRC)
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import os, time; from pathlib import Path; "
                 "from call_bridge.box_courier import CourierLock; "
                 "CourierLock(Path(os.environ['CALL_BRIDGE_COURIER_STATE']) / 'courier.lock').acquire(); "
                 "print('held', flush=True); time.sleep(30)"],
                env=env, stdout=subprocess.PIPE, text=True,
            )
            self.addCleanup(holder.kill)
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "held")
            script = Path(__file__).resolve().parents[1] / "scripts" / "box_courier.py"
            result = subprocess.run(
                [sys.executable, str(script), "--once"],
                env=env, capture_output=True, text=True, timeout=20,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("another courier is already running", result.stderr)
        self.assertNotIn("gw-secret", result.stderr)
        holder.kill()

    def test_exit_codes_name_the_failed_step(self) -> None:
        self.assertEqual(pass_exit_code({}), 0)
        self.assertEqual(pass_exit_code({"inflight_error": "http 401", "claim_error": "http 401"}), 2)
        self.assertEqual(pass_exit_code({"push_error": "http 401"}), 3)
        self.assertEqual(pass_exit_code({"receipt_error": "http 401", "push_error": "agents root not found"}), 4)
        line = format_pass({
            "reconciled": 0, "sent": 0, "pushed": 0,
            "inflight_error": "http 401", "claim_error": "http 401",
            "receipt_error": "", "push_error": "http 401",
        })
        self.assertIn("pass failed exit=4", line)
        self.assertIn("inflight=http 401", line)
        self.assertIn("claim=http 401", line)
        self.assertIn("directory=http 401", line)
        self.assertNotIn("token", line)

    def test_once_exits_nonzero_when_the_bridge_or_the_directory_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seat = root / "agents" / "rapi-agent"
            seat.mkdir(parents=True)
            (seat / "profile.json").write_text('{"name":"ラピ"}', encoding="utf-8")
            (root / "gateway.json").write_text(
                json.dumps({"port": 9, "token": "gw-test-do-not-log"}), encoding="utf-8",
            )
            (root / "bridge.token").write_text("bridge-test-do-not-log\n", encoding="utf-8")
            state = root / "state"
            state.mkdir()
            journal_path = state / "journal.json"
            armed = {"sess-kept:1": {"phase": "armed", "nonce": "nonce-kept", "claim_token": "token-kept"}}
            journal_path.write_text(json.dumps(armed), encoding="utf-8")

            def run(status_for) -> tuple[int, str]:
                journal_path.write_text(json.dumps(armed), encoding="utf-8")
                httpd = _status_server(status_for)
                host, port = httpd.server_address[:2]
                logs = _ListHandler()
                logger = logging.getLogger("call_bridge.box_courier")
                previous_level = logger.level
                logger.setLevel(logging.INFO)
                logger.addHandler(logs)
                env = {
                    "CALL_BRIDGE_GATEWAY_JSON": str(root / "gateway.json"),
                    "CALL_BRIDGE_TOKEN_FILE": str(root / "bridge.token"),
                    "CALL_BRIDGE_URL": f"http://{host}:{port}",
                    "CALL_BRIDGE_COURIER_STATE": str(state),
                    "CALL_BRIDGE_AGENTS_ROOT": str(root / "agents"),
                    "CALL_BRIDGE_COURIER_ID": "box",
                }
                try:
                    with mock.patch.dict(os.environ, env, clear=False):
                        code = courier_main(["--once"])
                finally:
                    logger.removeHandler(logs)
                    logger.setLevel(previous_level)
                    httpd.shutdown()
                    httpd.server_close()
                return code, "\n".join(logs.messages)

            both, both_log = run(lambda method, path: 401)
            self.assertEqual(both, 4, both_log)
            self.assertIn("pass failed exit=4", both_log)
            self.assertIn("inflight=http 401", both_log)
            self.assertIn("claim=http 401", both_log)
            self.assertIn("directory=http 401", both_log)
            self.assertNotIn("gw-test-do-not-log", both_log)
            self.assertNotIn("bridge-test-do-not-log", both_log)
            self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8")), armed)

            bridge_only, bridge_log = run(lambda method, path: 200 if method == "PUT" else 401)
            self.assertEqual(bridge_only, 2, bridge_log)
            self.assertIn("inflight=http 401", bridge_log)
            self.assertIn("claim=http 401", bridge_log)
            self.assertNotIn("directory=", bridge_log)
            self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8")), armed)

            directory_only, directory_log = run(lambda method, path: 401 if method == "PUT" else 200)
            self.assertEqual(directory_only, 3, directory_log)
            self.assertIn("directory=http 401", directory_log)
            self.assertNotIn("inflight=", directory_log)
            self.assertNotIn("claim=", directory_log)

            ok, ok_log = run(lambda method, path: 200)
            self.assertEqual(ok, 0, ok_log)
            self.assertIn("pass ok exit=0", ok_log)
            self.assertIn("pushed=1", ok_log)

    def test_lock_helper_reports_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "courier.lock"
            first = CourierLock(path)
            first.acquire()
            self.addCleanup(first.release)
            with self.assertRaises(CourierBusy):
                CourierLock(path).acquire()


def _status_server(status_for):
    """Local bridge double. ``status_for(method, path)`` is the HTTP status."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._reply(status_for("GET", self.path))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self._reply(status_for("POST", self.path))

        def do_PUT(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self._reply(status_for("PUT", self.path))

        def _reply(self, code: int) -> None:
            raw = b'{"ok":true,"deliveries":[]}' if code == 200 else b'{"ok":false}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class _World:
    """A bridge and a gateway the courier can dial. The bridge keeps the outbox."""

    def __init__(self) -> None:
        self.state = Path(tempfile.mkdtemp())
        self.queued: list[dict] = []
        self.inflight: list[dict] = []
        self.receipts: list[dict] = []
        self.prompts: list[dict] = []
        self._seq = 0
        self.bridge = ThreadingHTTPServer(("127.0.0.1", 0), self._bridge_handler())
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), self._gateway_handler())
        for httpd in (self.bridge, self.gateway):
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

    @property
    def bridge_url(self) -> str:
        host, port = self.bridge.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def gateway_url(self) -> str:
        host, port = self.gateway.server_address[:2]
        return f"http://{host}:{port}"

    def queue(self, prompt: str, agent_id: str, nonce: str) -> None:
        self._seq += 1
        self.queued.append({
            "session_id": "sess-1",
            "seq": self._seq,
            "nonce": nonce,
            "agent_id": agent_id,
            "member_name": "ラピ",
            "prompt": prompt,
        })

    def claim_for_test(self, courier_id: str) -> dict:
        item = self.queued.pop(0)
        item = {**item, "claim_token": f"token-{item['seq']}", "courier_id": courier_id}
        self.inflight.append(item)
        return item

    def close(self) -> None:
        for httpd in (self.bridge, self.gateway):
            httpd.shutdown()
            httpd.server_close()

    def _bridge_handler(self) -> type[BaseHTTPRequestHandler]:
        world = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self._json({"ok": True, "deliveries": list(world.inflight)})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                path = urlparse(self.path).path
                if path.endswith("/claim"):
                    deliveries = []
                    if world.queued:
                        deliveries.append(world.claim_for_test(body.get("courier_id", "box")))
                    self._json({"ok": True, "deliveries": deliveries})
                    return
                if path.endswith("/receipts"):
                    world.receipts.append(body)
                    seq = body["seq"]
                    world.inflight = [item for item in world.inflight if item["seq"] != seq]
                    state = "queued" if body.get("state") == "failed" and body.get("retryable") else body["state"]
                    self._json({"ok": True, "state": state, "seq": seq})
                    return
                self._json({"ok": False, "error": "not_found"}, 404)

            def do_PUT(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self._json({"ok": True, "count": 0})

            def _json(self, payload: dict, code: int = 200) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        return Handler

    def _gateway_handler(self) -> type[BaseHTTPRequestHandler]:
        world = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                world.prompts.append({
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                })
                raw = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        return Handler


if __name__ == "__main__":
    unittest.main()
