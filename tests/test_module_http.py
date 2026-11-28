"""The webhooks go through the authentication module's caller socket when an item is named."""
import base64
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from call_bridge import courier_watch, module_http, wake


class FakeModule:
    def __init__(self, reply):
        self.dir = tempfile.TemporaryDirectory(dir="/tmp")
        self.path = str(Path(self.dir.name) / "m.sock")
        self.requests = []
        self.reply = reply
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn, conn.makefile("rwb") as stream:
                self.requests.append(json.loads(stream.readline()))
                stream.write((json.dumps(self.reply) + "\n").encode())
                stream.flush()

    def close(self):
        self.server.close()
        self.dir.cleanup()


class ModuleWebhookTest(unittest.TestCase):
    def start(self, reply):
        module = FakeModule(reply)
        self.addCleanup(module.close)
        env = mock.patch.dict(os.environ, {
            module_http.SOCKET_ENV: module.path,
            wake.ITEM_ENV: "call-bridge-switchboard",
            courier_watch.ITEM_ENV: "call-bridge-courier-wake",
            "CALL_BRIDGE_GROKBOT_DELIVERY": "courier",
        })
        env.start()
        self.addCleanup(env.stop)
        for name in (*wake._URL_ENV, *wake._AUTH_ENV, courier_watch.URL_ENV, courier_watch.KEY_ENV):
            os.environ.pop(name, None)
        return module

    def test_switchboard_post_uses_item_and_root_path_without_url_or_key(self):
        module = self.start({"outcome": "granted", "status": 204})
        result = wake.notify_ring({"session_id": "s1"}, member_agent_id="a")
        self.assertEqual(result, {"status": "ok", "detail": "http 204"})
        request = module.requests[0]
        self.assertEqual((request["op"], request["name"], request["method"], request["path"]),
                         ("http", "call-bridge-switchboard", "POST", "/"))
        self.assertEqual(json.loads(base64.b64decode(request["body"]))["session_id"], "s1")
        self.assertNotIn("authorization", {k.lower() for k in request["headers"]})

    def test_refusal_is_an_error_result_not_a_raise(self):
        self.start({"outcome": "refused", "error": "secret-looking text"})
        result = wake.notify_link_down("box", "x")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("secret-looking text", json.dumps(result))

    def test_http_error_status_is_an_error(self):
        self.start({"outcome": "granted", "status": 401})
        self.assertEqual(wake.notify_ring({"session_id": "s"}, member_agent_id="a")["status"], "error")

    def test_missing_socket_is_an_error_result(self):
        self.start({"outcome": "granted", "status": 200})
        os.environ[module_http.SOCKET_ENV] = "/nonexistent/none.sock"
        self.assertEqual(wake.notify_ring({"session_id": "s"}, member_agent_id="a")["status"], "error")

    def test_item_without_socket_falls_back_to_unset_url(self):
        self.start({"outcome": "granted", "status": 200})
        os.environ.pop(module_http.SOCKET_ENV)
        self.assertEqual(wake.notify_ring({"session_id": "s"}, member_agent_id="a")["detail"], "webhook url unset")

    def test_courier_wake_enabled_and_posts_through_module(self):
        module = self.start({"outcome": "granted", "status": 200})
        self.assertTrue(courier_watch.enabled())
        courier_watch.notify({"event": "courier.silence"})
        self.assertEqual(module.requests[0]["name"], "call-bridge-courier-wake")
        self.assertEqual(json.loads(base64.b64decode(module.requests[0]["body"])), {"event": "courier.silence"})


if __name__ == "__main__":
    unittest.main()
