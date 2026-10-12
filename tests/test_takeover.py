"""受け取り係が、ほかの process の握っている通話を握り直す所。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from call_bridge import codex_delivery, local, receiver, setup

from test_stopped_conversation import Bridge, _conversation


class TakeoverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.temp.name, "CALL_BRIDGE_TOKEN": "test-token",
                                      "CALL_BRIDGE_MCP_URL": "http://localhost:18910/mcp"})
        env.start()
        self.addCleanup(env.stop)
        self.store = local.LocalStore(Path(self.temp.name))
        self.submit = AsyncMock(return_value="queue-id")
        for name, value in (("submit_reply", self.submit), ("_POLL_SECONDS", 0), ("_CONFIRM_AFTER_SECONDS", 0.0),
                            ("_TAKEOVER_SECONDS", 0.0), ("_YIELD_POLL_SECONDS", 0),
                            ("codex_conversation", AsyncMock(return_value=_conversation("completed"))),
                            ("codex_queued", AsyncMock(return_value=(False, _conversation("completed"))))):
            patcher = patch.object(local, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, mode: str = "queue") -> str:
        session_id = str(uuid.uuid4())
        self.store.add(session_id, "thread-a", Path(self.temp.name), "ナユタ", mode,
                       harness="codex" if mode == "queue" else "claude-code")
        return session_id

    def _hold(self, session_id: str) -> int:
        """頼みを読まない process（0.2.0 より前の作り）の代わりに、鍵を握ったままにする。"""
        fd = local._claim_session(session_id)
        self.assertIsNotNone(fd)
        self.addCleanup(os.close, fd)
        return fd

    async def _still_waiting(self, watcher: local.Watchers, session_id: str) -> None:
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(watcher.watch(session_id), 0.3)

    async def test_receiver_takes_a_call_from_the_process_that_held_it_and_delivers_the_reply(self):
        session_id = self._call()
        idle, mine = Bridge([]), Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        states: list[str] = []
        ask = self.store.ask_to_yield

        def asked(call):
            done = ask(call)
            states.append(self.store.subscription(call)["state"])
            return done

        with patch.object(local.httpx, "AsyncClient", side_effect=[idle, mine]), \
             patch.object(self.store, "ask_to_yield", asked):
            holder = asyncio.create_task(local.Watchers(self.store).watch(session_id))  # 会話の中の通話MCP
            while not idle.peeks:  # 握って、見回り始めた
                await asyncio.sleep(0)
            await asyncio.wait_for(local.Watchers(self.store, receiver=True).watch(session_id), 5)
            await asyncio.wait_for(holder, 5)
        self.assertEqual(states, ["yielding"])
        self.assertEqual(self.submit.await_count, 1)
        self.assertEqual((mine.receipts[0]["seq"], mine.receipts[0]["state"]), (2, "submitted"))
        self.assertEqual(idle.receipts, [])
        self.assertEqual(self.store.subscription(session_id)["state"], "closed")

    async def test_only_the_receiver_asks_and_never_for_a_call_its_conversation_fetches_by_itself(self):
        shared, manual = self._call(), self._call("manual")
        self._hold(shared)
        self._hold(manual)
        await self._still_waiting(local.Watchers(self.store), shared)  # 会話の中の通話MCPは、待つだけ
        await self._still_waiting(local.Watchers(self.store, receiver=True), manual)
        self.assertFalse(self.store.ask_to_yield(manual))
        self.assertEqual([self.store.subscription(call)["state"] for call in (shared, manual)], ["active", "active"])

    async def test_process_that_does_not_let_go_keeps_the_call_and_the_call_stays_active(self):
        session_id = self._call()
        self._hold(session_id)
        asked: list[str] = []
        ask = self.store.ask_to_yield

        def count(call):
            asked.append(call)
            return ask(call)

        with patch.object(local, "_YIELD_SECONDS", 0.0), patch.object(self.store, "ask_to_yield", count), \
             self.assertLogs("call_bridge.local", "WARNING") as logs:
            await self._still_waiting(local.Watchers(self.store, receiver=True), session_id)
        self.assertEqual(len(asked), local._TAKEOVER_ATTEMPTS)
        self.assertIn("does not let go", logs.output[0])
        self.assertEqual(self.store.subscription(session_id)["state"], "active")

    async def test_request_is_withdrawn_when_the_receiver_stops_while_waiting(self):
        session_id = self._call()
        self._hold(session_id)
        with patch.object(local, "_YIELD_SECONDS", 60.0), patch.object(local, "_YIELD_POLL_SECONDS", 0.01):
            task = asyncio.create_task(local.Watchers(self.store, receiver=True).watch(session_id))
            while self.store.subscription(session_id)["state"] != "yielding":
                await asyncio.sleep(0.01)
            # 待っている間も、通話の持ち主と、同じ会話に付いた通話を探す側からは、生きている通話に見える。
            self.assertEqual(self.store.status(session_id)["state"], "active")
            self.assertEqual([row["session_id"] for row in self.store.bound_to("codex", "thread-a", session_id)],
                             [session_id])
            self.assertEqual(self.store.active(), [])  # 新しく待ち始める見張りは居ない
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.store.subscription(session_id)["state"], "active")

    async def test_new_receiver_reactivates_calls_the_previous_one_left_mid_takeover(self):
        session_id, closed = self._call(), self._call()
        self.assertTrue(self.store.ask_to_yield(session_id))
        self.store.stop(closed, "closed")
        watched: list[str] = []

        class Recorder:
            def __init__(self, _store, receiver=False):
                pass

            def start(self, call, claimed=None):
                watched.append(call)

            async def close(self):
                pass

        with patch.object(local, "Watchers", Recorder), patch.object(receiver, "_SCAN_SECONDS", 0.01):
            await receiver.run(scans=1)
        self.assertEqual(watched, [session_id])
        self.assertEqual([self.store.subscription(call)["state"] for call in (session_id, closed)], ["active", "closed"])


class StoredCodexTest(unittest.TestCase):
    """控えた Codex の場所。版つきの場所を控えた端末では、Codex が古い版を片付けると無くなる。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current = self.root / "bin" / ("codex.exe" if os.name == "nt" else "codex")
        self.current.parent.mkdir()
        self.current.write_text("", encoding="utf-8")
        self.current.chmod(0o700)
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.temp.name, "PATH": str(self.current.parent)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CODEX_CLI_PATH", None)

    def test_missing_stored_codex_falls_back_to_the_one_on_path(self) -> None:
        gone = self.root / "releases" / "0.155.1" / "codex"
        setup._write_json(self.root / "config.json", {"codex_binary": str(gone)})
        self.assertEqual(Path(codex_delivery.codex_binary()), self.current)
        kept = self.root / "kept"
        kept.write_text("", encoding="utf-8")
        setup._write_json(self.root / "config.json", {"codex_binary": str(kept)})
        self.assertEqual(codex_delivery.codex_binary(), str(kept))  # ある間は、控えた物を使う

    def test_delivery_package_is_not_pointed_at_a_codex_that_is_gone(self) -> None:
        cli = self.root / "cli"
        cli.write_text("", encoding="utf-8")
        config = {"steer_cli": str(cli), "codex_binary": str(self.root / "releases" / "0.155.1" / "codex")}
        os.environ.pop("CODEX_BIN", None)
        with patch.object(codex_delivery, "current_steer_cli", return_value=cli):
            _command, env = codex_delivery._steer_command(config)
        self.assertEqual(Path(env["CODEX_BIN"]), self.current)
