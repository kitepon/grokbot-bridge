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
import psutil

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

    async def test_call_info_exposes_uncertain_hook_delivery(self):
        store = local.LocalStore(Path(self.temp.name))
        session_id, thread_id = str(uuid.uuid4()), str(uuid.uuid4())
        store.add(session_id, thread_id, Path(self.temp.name), "ラピ")
        delivery_id, _ = store.reserve(session_id, 1)
        store.submitted(session_id, 1)
        claim = Path(self.temp.name) / "codex-inputs" / thread_id / "claims" / f"{delivery_id}.json"
        codex_delivery._write_json(claim, {"state": "unknown", "text": "GrokBot の返事"})
        status = store.status(session_id)
        self.assertEqual(status["deliveries"][0]["state"], "unknown")
        self.assertEqual(status["deliveries"][0]["error"], "CODEX_HOOK_DELIVERY_UNCONFIRMED")
        self.assertEqual(status["after_seq"], 1)


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

    async def test_idle_queue_consumption_cleans_settled_owner(self):
        thread_id, delivery_id = str(uuid.uuid4()), str(uuid.uuid4())
        pending = codex_delivery._pending_dir(thread_id) / f"{delivery_id}.json"
        settled = pending.parent.parent / "settled" / pending.name
        codex_delivery._write_json_once(pending, {"delivery_id": delivery_id})
        codex_delivery._write_json(settled, {"delivery_id": delivery_id})

        class EmptyRPC:
            def __init__(self, *_args, **_kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): pass
            async def request(self, _method, _params): return {"data": [], "nextCursor": None}

        with patch.object(codex_delivery, "CodexRPC", EmptyRPC):
            output, claims = await codex_delivery.claim_hook_replies({
                "session_id": thread_id, "turn_id": "turn-1", "hook_event_name": "PostToolUse",
            })
        self.assertEqual((output, claims), ({}, []))
        self.assertFalse(pending.exists())
        self.assertFalse(settled.exists())

    async def test_interrupted_queue_claim_is_unknown_and_keeps_body(self):
        thread_id, delivery_id = str(uuid.uuid4()), str(uuid.uuid4())
        body = "GrokBot の返事"
        source = codex_delivery._pending_dir(thread_id) / f"{delivery_id}.json"
        codex_delivery._write_json_once(source, {
            "thread_id": thread_id, "delivery_id": delivery_id,
            "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
        })

        class FailingRPC:
            def __init__(self, *_args, **_kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): pass
            async def request(self, method, _params):
                if method == "thread/queue/list":
                    return {"data": [{"id": "queued", "clientUserMessageId": delivery_id,
                                      "input": [{"type": "text", "text": body}]}], "nextCursor": None}
                raise codex_delivery.DeliveryError("CODEX_REQUEST_TIMEOUT", "削除結果が不明")

        with patch.object(codex_delivery, "CodexRPC", FailingRPC):
            with self.assertRaisesRegex(codex_delivery.DeliveryError, "CODEX_REQUEST_TIMEOUT"):
                await codex_delivery.claim_hook_replies({
                    "session_id": thread_id, "turn_id": "turn-1", "hook_event_name": "Stop",
                })
        claim = source.parent.parent / "claims" / source.name
        self.assertEqual(json.loads(claim.read_text())["text"], body)
        self.assertEqual(codex_delivery.hook_delivery_state(thread_id, delivery_id), "unknown")

    async def test_interrupted_hook_process_is_reported_unknown(self):
        thread_id, delivery_id = str(uuid.uuid4()), str(uuid.uuid4())
        claim = codex_delivery._pending_dir(thread_id).parent / "claims" / f"{delivery_id}.json"
        process = psutil.Process()
        codex_delivery._write_json(claim, {
            "state": "deleting", "pid": process.pid, "create_time": process.create_time(),
            "text": "GrokBot の返事",
        })
        self.assertEqual(codex_delivery.hook_delivery_state(thread_id, delivery_id), "sending")
        codex_delivery._write_json(claim, {
            "state": "deleting", "pid": process.pid, "create_time": process.create_time() - 1,
            "text": "GrokBot の返事",
        })
        self.assertEqual(codex_delivery.hook_delivery_state(thread_id, delivery_id), "unknown")


class ProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_preexisting_codex_process_requires_restart(self):
        current = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
        config = {"stale_processes": [current]}
        self.assertTrue(codex_delivery.restart_required(config))
        with self.assertRaisesRegex(codex_delivery.DeliveryError, "CODEX_STEER_RESTART_REQUIRED"):
            codex_delivery.assert_parent_current(config)
        self.assertFalse(codex_delivery.restart_required({
            "stale_processes": [{**current, "create_time": current["create_time"] - 1}],
        }))

    async def test_setup_migrates_old_config_to_restart_check(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            state.mkdir()
            old = {"enabled": True, "mcp_name": "call-bridge", "mcp_url": "https://example.com/mcp",
                   "token_env": "CALL_BRIDGE_TOKEN", "hook_command": "hook"}
            setup._write_json(state / "config.json", old)
            current = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
            transport = {"transport": {"type": "stdio", "args": ["-m", "call_bridge.local"]}}
            with patch.dict(os.environ, {"CALL_BRIDGE_STATE": str(state), "CODEX_HOME": root,
                                      "CALL_BRIDGE_TOKEN": "test-token"}), \
                 patch.object(setup, "_find_existing", return_value=("call-bridge", transport)), \
                 patch.object(setup, "_existing", return_value=transport), \
                 patch.object(setup, "_command", return_value="hook"), \
                 patch.object(setup, "codex_binary", return_value="/bin/echo"), \
                 patch.object(setup, "codex_processes", return_value=[current]), \
                 patch.object(setup, "_backup_codex_config"), \
                 patch.object(setup, "_merge_hooks", return_value=False), \
                 patch.object(setup, "_verify_hooks", new_callable=AsyncMock):
                self.assertEqual((await setup.enable())["status"], "restart_required")
                self.assertEqual((await setup.status())["status"], "restart_required")
            self.assertEqual(json.loads((state / "config.json").read_text())["stale_processes"], [current])


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
