"""返信経路、キューの所有判定、Codex hook 出力を確認する。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from call_bridge import codex_delivery, local, setup


class FakeHTTP:
    def __init__(self, *_args, **_kwargs):
        self.polls: list[int] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def get(self, url, params):
        self.polls.append(params["after_seq"])
        if params["after_seq"] == 0:
            body = {"ok": True, "status": "open", "messages": [
                {"seq": 2, "message": "一通目"}, {"seq": 4, "message": "二通目"},
            ]}
        else:
            body = {"ok": True, "status": "hungup", "messages": []}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))


class LocalDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "CALL_BRIDGE_STATE": self.temp.name,
            "CALL_BRIDGE_TOKEN": "test-token",
            "CALL_BRIDGE_MCP_URL": "http://localhost:18910/mcp",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_two_replies_are_delivered_in_order_then_watch_closes(self):
        store = local.LocalStore(Path(self.temp.name))
        session_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        store.add(session_id, thread_id, Path(self.temp.name), "ラピ")
        fake = FakeHTTP()
        send = AsyncMock(return_value="queue-id")
        with patch.object(local.httpx, "AsyncClient", return_value=fake), \
             patch.object(local, "submit_reply", send), \
             patch.object(local, "_POLL_SECONDS", 0):
            await local.Watchers(store).watch(session_id)
        self.assertEqual(fake.polls, [0, 4])
        self.assertEqual(send.await_count, 2)
        self.assertIn("一通目", send.await_args_list[0].args[3])
        self.assertIn("二通目", send.await_args_list[1].args[3])
        self.assertEqual(store.status(session_id)["state"], "closed")
        self.assertEqual(store.status(session_id)["after_seq"], 4)

    async def test_uncertain_queue_result_stops_without_resending(self):
        store = local.LocalStore(Path(self.temp.name))
        session_id = str(uuid.uuid4())
        store.add(session_id, str(uuid.uuid4()), Path(self.temp.name), "ラピ")
        send = AsyncMock(side_effect=codex_delivery.DeliveryError(
            "CODEX_REQUEST_TIMEOUT", "送信後の応答なし", outcome_unknown=True))
        with patch.object(local.httpx, "AsyncClient", return_value=FakeHTTP()), \
             patch.object(local, "submit_reply", send):
            await local.Watchers(store).watch(session_id)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(store.status(session_id)["state"], "unknown")
        self.assertEqual(store.status(session_id)["deliveries"][0]["state"], "unknown")

    async def test_parent_is_taken_from_codex_request_metadata(self):
        thread_id = str(uuid.uuid4())
        ctx = SimpleNamespace(
            session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=SimpleNamespace(name="codex-mcp-client"))),
            request_context=SimpleNamespace(meta=SimpleNamespace(model_extra={"threadId": thread_id})),
        )
        self.assertEqual(local._parent(ctx)[0], thread_id)

    async def test_only_one_process_owns_a_call_watcher(self):
        session_id = str(uuid.uuid4())
        first = local._claim_session(session_id)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(local._claim_session(session_id))
        finally:
            os.close(first)
        second = local._claim_session(session_id)
        self.assertIsNotNone(second)
        os.close(second)

    async def test_local_mcp_reads_private_token_when_codex_does_not_pass_environment(self):
        root = Path(self.temp.name)
        setup._write_json(root / "config.json", {
            "enabled": True, "token_env": "CALL_BRIDGE_TOKEN"})
        setup._write_json(root / "auth.json", {"token": "fictional-stored-token"})
        self.assertEqual((root / "auth.json").stat().st_mode & 0o777, 0o600)
        with patch.dict(os.environ, {"CALL_BRIDGE_TOKEN": ""}):
            self.assertEqual(local._headers(), {"Authorization": "Bearer fictional-stored-token"})
        self.assertEqual(local._headers(), {"Authorization": "Bearer test-token"})
        setup._write_json(root / "config.json", {
            "enabled": False, "token_env": "CALL_BRIDGE_TOKEN"})
        with patch.dict(os.environ, {"CALL_BRIDGE_TOKEN": ""}):
            with self.assertRaisesRegex(codex_delivery.DeliveryError, "BRIDGE_TOKEN_MISSING"):
                local._headers()


class HookTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_hook_claims_only_its_own_queued_reply(self):
        thread_id = str(uuid.uuid4())
        delivery_id = str(uuid.uuid4())
        body = "GrokBot の返事"
        source = codex_delivery._pending_dir(thread_id) / f"{delivery_id}.json"
        codex_delivery._write_json_once(source, {
            "thread_id": thread_id, "delivery_id": delivery_id,
            "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
        })
        entries = [
            {"id": "foreign", "clientUserMessageId": str(uuid.uuid4()),
             "input": [{"type": "text", "text": "他の入力"}]},
            {"id": "ours", "clientUserMessageId": delivery_id,
             "input": [{"type": "text", "text": body}]},
        ]

        class FakeRPC:
            def __init__(self, *_args, **_kwargs):
                self.deleted = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def request(self, method, params):
                if method == "thread/queue/list":
                    return {"data": entries, "nextCursor": None}
                self.deleted.append(params["queuedSubmissionId"])
                return {"deleted": True}

        with patch.object(codex_delivery, "CodexRPC", FakeRPC):
            output, claims = await codex_delivery.claim_hook_replies({
                "session_id": thread_id, "turn_id": "turn-1", "hook_event_name": "PostToolUse",
            })
        self.assertEqual(output["hookSpecificOutput"]["additionalContext"], body)
        self.assertEqual(len(claims), 1)
        self.assertFalse(source.exists())


class SetupTest(unittest.TestCase):
    def test_hook_merge_preserves_other_products(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"CALL_BRIDGE_STATE": temp}):
                file = Path(temp) / "hooks.json"
                file.write_text(json.dumps({"hooks": {
                    "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other-hook"}]}],
                }}), encoding="utf-8")
                self.assertTrue(setup._merge_hooks(file, "python -m call_bridge.codex_delivery"))
                value = json.loads(file.read_text(encoding="utf-8"))
                self.assertEqual(value["hooks"]["PostToolUse"][0]["hooks"][0]["command"], "other-hook")
                self.assertFalse(setup._merge_hooks(file, "python -m call_bridge.codex_delivery"))


if __name__ == "__main__":
    unittest.main()
