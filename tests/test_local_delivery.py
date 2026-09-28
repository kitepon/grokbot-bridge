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

    async def test_exec_reply_is_reported_as_persisted_injection(self):
        store = local.LocalStore(Path(self.temp.name))
        session_id = str(uuid.uuid4())
        store.add(session_id, str(uuid.uuid4()), Path(self.temp.name), "ラピ")
        with patch.object(local.httpx, "AsyncClient", return_value=FakeHTTP()), \
             patch.object(local, "submit_reply", AsyncMock(return_value="injected")), \
             patch.object(local, "_POLL_SECONDS", 0):
            await local.Watchers(store).watch(session_id)
        self.assertEqual([row["state"] for row in store.status(session_id)["deliveries"]],
                         ["injected", "injected"])

    async def test_busy_exec_writer_defers_same_reply_until_next_poll(self):
        store = local.LocalStore(Path(self.temp.name))
        session_id = str(uuid.uuid4())
        store.add(session_id, str(uuid.uuid4()), Path(self.temp.name), "ラピ")
        send = AsyncMock(side_effect=["deferred", "injected", "injected"])
        with patch.object(local.httpx, "AsyncClient", return_value=FakeHTTP()), \
             patch.object(local, "submit_reply", send), \
             patch.object(local, "_POLL_SECONDS", 0):
            await local.Watchers(store).watch(session_id)
        self.assertEqual(send.await_count, 3)
        self.assertEqual(send.await_args_list[0].args[2], send.await_args_list[1].args[2])
        self.assertEqual(store.status(session_id)["after_seq"], 4)
        self.assertEqual([row["state"] for row in store.status(session_id)["deliveries"]],
                         ["injected", "injected"])

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

    async def test_call_open_forwards_systems_to_remote_mcp(self):
        thread_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        tool = next(item for item in await local.mcp.list_tools() if item.name == "call_open")
        self.assertIn("member_system", tool.inputSchema["properties"])
        self.assertIn("local_system", tool.inputSchema["properties"])
        with patch.object(local, "_parent", return_value=(thread_id, Path(self.temp.name))), \
             patch.object(local, "verify_parent", new_callable=AsyncMock), \
             patch.object(local, "_remote_tool", new_callable=AsyncMock,
                          return_value={"session_id": session_id, "status": "ringing"}) as remote, \
             patch.object(local.store, "add") as add, \
             patch.object(local, "_start_watch") as start:
            result = await local.call_open(
                "caller", "発信者", "bot-1", "相談", "bellteam", "grokbot", ctx=object())

        remote.assert_awaited_once_with("call_open", {
            "local_id": "caller", "local_label": "発信者", "member_name": "bot-1",
            "purpose": "相談", "member_system": "bellteam", "local_system": "grokbot",
        })
        add.assert_called_once_with(session_id, thread_id, Path(self.temp.name), "bot-1", "queue",
                                    "bellteam")
        start.assert_called_once_with(session_id)
        self.assertEqual(result["parent_delivery"]["state"], "watching")

    async def test_exec_call_open_waits_for_parent_prompt(self):
        session_id, thread_id = str(uuid.uuid4()), str(uuid.uuid4())
        store = local.LocalStore(Path(self.temp.name))
        with patch.object(local, "_parent", return_value=(thread_id, Path(self.temp.name))), \
             patch.object(local, "verify_parent", new_callable=AsyncMock, return_value="exec"), \
             patch.object(local, "_remote_tool", new_callable=AsyncMock,
                          return_value={"session_id": session_id}), \
             patch.object(local, "store", store), \
             patch.object(local, "_launch_exec_watcher") as launch, \
             patch.object(local.watchers, "start") as in_process:
            result = await local.call_open("caller", "発信者", "bot-1", ctx=object())
        self.assertEqual(store.subscription(session_id)["delivery_mode"], "exec")
        self.assertEqual(result["parent_delivery"]["state"], "awaiting_parent_prompt")
        launch.assert_not_called()
        in_process.assert_not_called()

    async def test_exec_prompt_hook_emits_stored_replies_once(self):
        thread_id, session_id = str(uuid.uuid4()), str(uuid.uuid4())
        store = local.LocalStore(Path(self.temp.name))
        store.add(session_id, thread_id, codex_delivery.codex_home(), "ベル", "exec")

        class ReplyHTTP(FakeHTTP):
            async def get(self, url, params):
                self.polls.append(params["after_seq"])
                body = {"ok": True, "status": "open", "messages": [
                    {"seq": 2, "message": "返信の証拠"},
                ] if params["after_seq"] == 0 else []}
                return httpx.Response(200, json=body, request=httpx.Request("GET", url))

        event = {"session_id": thread_id, "turn_id": "turn-1",
                 "hook_event_name": "UserPromptSubmit", "prompt": "再開"}
        with patch.object(local.httpx, "AsyncClient", return_value=ReplyHTTP()):
            output, reserved, closed = await codex_delivery.claim_exec_replies(event)
        self.assertIn("返信の証拠", output["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(reserved, [(session_id, 2)])
        self.assertEqual(closed, [])
        for sid, seq in reserved:
            store.submitted(sid, seq, "injected")
        self.assertEqual(store.status(session_id)["after_seq"], 2)
        self.assertEqual(store.status(session_id)["deliveries"][0]["state"], "injected")

        with patch.object(local.httpx, "AsyncClient", return_value=ReplyHTTP()):
            second, reserved, _closed = await codex_delivery.claim_exec_replies(event)
        self.assertEqual(second, {})
        self.assertEqual(reserved, [])

    async def test_call_open_keeps_grokbot_default_and_leaves_local_system_to_the_bridge(self):
        session_id = str(uuid.uuid4())
        with patch.object(local, "_parent", return_value=(str(uuid.uuid4()), Path(self.temp.name))), \
             patch.object(local, "verify_parent", new_callable=AsyncMock), \
             patch.object(local, "_remote_tool", new_callable=AsyncMock,
                          return_value={"session_id": session_id}) as remote, \
             patch.object(local.store, "add"), \
             patch.object(local, "_start_watch"):
            await local.call_open("caller", "発信者", "ラピ", ctx=object())

        self.assertEqual(remote.await_args.args[1]["member_system"], "grokbot")
        self.assertNotIn("local_system", remote.await_args.args[1])

    async def test_queue_reply_names_the_member_system(self):
        store = local.LocalStore(Path(self.temp.name))
        headers = {}
        for system, expected in (("grokbot", "GrokBot の返信です。"), ("bellteam", "BellTeam の返信です。"),
                                 (None, "通話の返信です。")):
            session_id = str(uuid.uuid4())
            store.add(session_id, str(uuid.uuid4()), Path(self.temp.name), "相手", member_system=system)
            send = AsyncMock(return_value="queue-id")
            with patch.object(local.httpx, "AsyncClient", return_value=FakeHTTP()), \
                 patch.object(local, "submit_reply", send), \
                 patch.object(local, "_POLL_SECONDS", 0):
                await local.Watchers(store).watch(session_id)
            headers[system] = send.await_args_list[0].args[3]
            self.assertTrue(headers[system].startswith(expected), headers[system])
        self.assertNotIn("GrokBot", headers["bellteam"])

    async def test_exec_reply_names_bellteam_member(self):
        thread_id, session_id = str(uuid.uuid4()), str(uuid.uuid4())
        store = local.LocalStore(Path(self.temp.name))
        store.add(session_id, thread_id, codex_delivery.codex_home(), "トロニー", "exec", "bellteam")
        event = {"session_id": thread_id, "turn_id": "turn-1",
                 "hook_event_name": "UserPromptSubmit", "prompt": "再開"}
        with patch.object(local.httpx, "AsyncClient", return_value=FakeHTTP()):
            output, _reserved, _closed = await codex_delivery.claim_exec_replies(event)
        self.assertTrue(output["hookSpecificOutput"]["additionalContext"].startswith("BellTeam の返信です。"))

    async def test_store_adds_member_system_to_existing_database(self):
        path = Path(self.temp.name) / "local.sqlite"
        import sqlite3
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE subscriptions (session_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
                       "codex_home TEXT NOT NULL, member_name TEXT NOT NULL, after_seq INTEGER NOT NULL DEFAULT 0, "
                       "state TEXT NOT NULL DEFAULT 'active', last_error TEXT, "
                       "delivery_mode TEXT NOT NULL DEFAULT 'queue')")
            db.execute("INSERT INTO subscriptions(session_id, thread_id, codex_home, member_name) "
                       "VALUES('old', 't', 'h', 'ラピ')")
        store = local.LocalStore(Path(self.temp.name))
        self.assertIsNone(store.subscription("old")["member_system"])
        with self.assertRaises(ValueError):
            store.add(str(uuid.uuid4()), "t", Path(self.temp.name), "x", member_system="other")

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

    async def test_exec_reply_injects_once_into_persisted_history(self):
        thread_id, delivery_id = str(uuid.uuid4()), str(uuid.uuid4())
        calls = []

        class FakeRPC:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def request(self, method, params):
                calls.append((method, params))
                if method == "thread/read":
                    return {"thread": {"id": thread_id, "source": "exec"}}
                return {}

        with patch.object(codex_delivery, "CodexRPC", FakeRPC):
            self.assertEqual(await codex_delivery.submit_reply(
                thread_id, Path(self.temp.name), delivery_id, "返信本文"), "injected")
            with self.assertRaisesRegex(codex_delivery.DeliveryError, "DELIVERY_ALREADY_STARTED"):
                await codex_delivery.submit_reply(
                    thread_id, Path(self.temp.name), delivery_id, "返信本文")
        self.assertEqual([method for method, _ in calls[:3]],
                         ["thread/read", "thread/resume", "thread/inject_items"])
        self.assertEqual(calls[2][1]["items"][0]["content"][0]["text"], "返信本文")
        self.assertFalse((codex_delivery._pending_dir(thread_id) / f"{delivery_id}.json").exists())

    async def test_busy_exec_writer_defers_before_any_injection_attempt(self):
        thread_id, delivery_id = str(uuid.uuid4()), str(uuid.uuid4())

        class FakeRPC:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def request(self, method, _params):
                if method == "thread/read":
                    return {"thread": {"id": thread_id, "source": "exec"}}
                if method == "thread/resume":
                    raise codex_delivery.DeliveryError(
                        "CODEX_REQUEST_REJECTED", "thread already has an active writer")
                raise AssertionError("injection must not run while the parent is the writer")

        with patch.object(codex_delivery, "CodexRPC", FakeRPC):
            self.assertEqual(await codex_delivery.submit_reply(
                thread_id, Path(self.temp.name), delivery_id, "返信本文"), "deferred")
        marker = Path(self.temp.name) / "codex-inputs" / thread_id / "injections" / f"{delivery_id}.json"
        self.assertFalse(marker.exists())

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
    async def test_codex_in_other_pid_namespace_does_not_require_restart(self):
        current = {"pid": os.getpid(), "create_time": psutil.Process().create_time()}
        foreign = SimpleNamespace(pid=current["pid"], info={
            "name": "codex", "exe": "/usr/bin/codex", "create_time": current["create_time"],
        })
        with patch.object(codex_delivery, "_same_pid_namespace", return_value=False), \
             patch.object(codex_delivery.psutil, "process_iter", return_value=[foreign]):
            self.assertEqual(codex_delivery.codex_processes(), [])
            self.assertFalse(codex_delivery.restart_required({"stale_processes": [current]}))

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
    def test_node_codex_uses_recorded_runtime_with_minimal_path(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            state.mkdir()
            script = Path(root) / "codex.js"
            script.write_text("", encoding="utf-8")
            node = Path(root) / "node"
            node.write_text("", encoding="utf-8")
            setup._write_json(state / "config.json", {
                "codex_binary": str(script), "node_binary": str(node),
            })
            with patch.dict(os.environ, {"CALL_BRIDGE_STATE": str(state), "PATH": root}, clear=True):
                self.assertEqual(codex_delivery.codex_command(), [str(node), str(script)])

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
