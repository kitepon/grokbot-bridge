"""公式 Codex を隔離起動し、進行中と待機中の返信配送を確認する。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from call_bridge.codex_delivery import CodexRPC, submit_reply
from call_bridge.setup import _command, _merge_hooks, _replace_mcp, _verify_hooks, _write_json


class ModelHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    arrived = threading.Event()
    release = threading.Event()

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        size = int(self.headers["Content-Length"])
        value = json.loads(self.rfile.read(size))
        self.requests.append(value)
        number = len(self.requests)
        if number == 1:
            self.arrived.set()
            self.release.wait(15)
        response_id = f"response-{number}"
        item = {"type": "message", "role": "assistant", "id": f"message-{number}",
                "content": [{"type": "output_text", "text": f"試験応答{number}"}]}
        events = [
            {"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": response_id,
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}},
        ]
        body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(os.environ.get("CALL_BRIDGE_TEST_CODEX_BINARY"), "公式 Codex binary の指定時だけ実行")
class OfficialCodexTest(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_switch_preserves_other_server_settings(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / "codex"
            home.mkdir()
            config = home / "config.toml"
            config.write_text('''[mcp_servers.other]
command = "echo"
args = ["hello"]
enabled = false
required = true

[mcp_servers.call-bridge]
url = "https://example.com/mcp"
bearer_token_env_var = "TEST_TOKEN"
''', encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": str(home),
                                      "CODEX_CLI_PATH": os.environ["CALL_BRIDGE_TEST_CODEX_BINARY"]}):
                await _replace_mcp("call-bridge", {"command": "python", "args": ["-m", "call_bridge.local"]})
            import tomllib
            servers = tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]
            self.assertEqual(servers["other"], {
                "command": "echo", "args": ["hello"], "enabled": False, "required": True,
            })
            self.assertEqual(servers["call-bridge"], {
                "command": "python", "args": ["-m", "call_bridge.local"],
            })

    async def test_active_parent_receives_reply_once_in_same_turn(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            home = base / "codex"
            home.mkdir()
            ModelHandler.requests = []
            ModelHandler.arrived = threading.Event()
            ModelHandler.release = threading.Event()
            server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}"
            home.joinpath("config.toml").write_text(f'''model = "mock-model"
model_provider = "mock_provider"
approval_policy = "never"
sandbox_mode = "read-only"
cli_auth_credentials_store = "file"
mcp_oauth_credentials_store = "file"
chatgpt_base_url = "{url}"
[model_providers.mock_provider]
name = "配送試験専用モデル"
base_url = "{url}/v1"
wire_api = "responses"
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
''', encoding="utf-8")
            env = {
                "HOME": root, "CODEX_HOME": str(home), "CALL_BRIDGE_STATE": str(base / "state"),
                "CODEX_CLI_PATH": os.environ["CALL_BRIDGE_TEST_CODEX_BINARY"],
            }
            try:
                with patch.dict(os.environ, env):
                    command = _command()
                    _merge_hooks(home / "hooks.json", command)
                    await _verify_hooks(command, approve=True)
                    _write_json(base / "state" / "config.json", {
                        "enabled": True, "hook_command": command,
                        "codex_binary": env["CODEX_CLI_PATH"],
                    })
                    async with CodexRPC(home) as parent:
                        started = await parent.request("thread/start", {"cwd": root})
                        thread_id = started["thread"]["id"]
                        await parent.request("turn/start", {"threadId": thread_id,
                            "input": [{"type": "text", "text": "最初の試験入力"}]})
                        arrived = await asyncio.to_thread(ModelHandler.arrived.wait, 10)
                        self.assertTrue(arrived, "最初のモデル要求が来ない")
                        marker = "CALL_BRIDGE_REPLY_" + uuid.uuid4().hex
                        await submit_reply(thread_id, home, str(uuid.uuid4()), marker)
                        ModelHandler.release.set()
                        for _ in range(200):
                            if len(ModelHandler.requests) >= 2:
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(len(ModelHandler.requests), 2)
                        self.assertEqual(json.dumps(ModelHandler.requests[1]).count(marker), 1)
                        for _ in range(100):
                            history = (await parent.request("thread/read", {
                                "threadId": thread_id, "includeTurns": True,
                            }))["thread"]
                            if history.get("turns") and history["turns"][-1].get("status") == "completed":
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(len(history["turns"]), 1, "返信は同じターンへ届く")
                        idle_marker = "CALL_BRIDGE_IDLE_" + uuid.uuid4().hex
                        await submit_reply(thread_id, home, str(uuid.uuid4()), idle_marker)
                        for _ in range(200):
                            if len(ModelHandler.requests) >= 3:
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(len(ModelHandler.requests), 3)
                        self.assertEqual(json.dumps(ModelHandler.requests[2]).count(idle_marker), 1)
                        for _ in range(100):
                            history = (await parent.request("thread/read", {
                                "threadId": thread_id, "includeTurns": True,
                            }))["thread"]
                            if len(history.get("turns", [])) == 2 and history["turns"][-1].get("status") == "completed":
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(len(history["turns"]), 2, "待機中の返信は次のターンへ届く")
            finally:
                ModelHandler.release.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
