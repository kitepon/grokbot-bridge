"""生きている Claude Code の会話へ、返信を自動で渡す所。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from call_bridge import claude_channel, harness_setup, local, relaunch
from call_bridge.codex_delivery import DeliveryError

from test_stopped_conversation import Bridge, FakeTerminal

CHANNEL = "11111111-1111-4111-8111-111111111111"


class ChannelWatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "openlogicool"
        self.folder.mkdir()
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.temp.name, "CALL_BRIDGE_TOKEN": "test-token",
                                      "CALL_BRIDGE_MCP_URL": "http://localhost:18910/mcp"})
        env.start()
        self.addCleanup(env.stop)
        self.store = local.LocalStore(Path(self.temp.name))
        self.terminal = FakeTerminal()
        self.send = AsyncMock()
        self.state = AsyncMock(return_value=("emitted", False))
        self.withdraw = AsyncMock(return_value=True)
        self.close = AsyncMock()
        self.bodies = AsyncMock(side_effect=lambda _session, seq: f"返事{seq}")
        for target, name, value in ((local.claude_channel, "send", self.send),
                                    (local.claude_channel, "delivery_state", self.state),
                                    (local.claude_channel, "withdraw", self.withdraw),
                                    (local.claude_channel, "close", self.close),
                                    (local, "fetch_body", self.bodies),
                                    (local, "_POLL_SECONDS", 0), (local, "_CHANNEL_CONFIRM_SECONDS", 0.0),
                                    (local, "_RECHECK_SECONDS", 0.0),
                                    (local.aiterm, "connect", self.terminal.connect),
                                    (relaunch.aiterm, "connect", self.terminal.connect)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self) -> str:
        session_id = str(uuid.uuid4())
        self.store.add(session_id, CHANNEL, Path(""), "リリーバイス", "channel", "bellteam", "claude-code", str(self.folder))
        return session_id

    async def _watch(self, session_id: str, bridge: Bridge, receiver: bool = False) -> None:
        with patch.object(local.httpx, "AsyncClient", return_value=bridge):
            await asyncio.wait_for(local.Watchers(self.store, receiver=receiver).watch(session_id), 5)

    def _states(self, session_id: str) -> list[tuple[int, str]]:
        return [(row["seq"], row["state"]) for row in self.store.status(session_id)["deliveries"]]

    async def test_reply_goes_into_the_living_conversation_and_the_sender_sees_it_started(self):
        session_id = self._call()
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge)
        self.assertEqual(self.send.await_count, 1)
        channel, _delivery, text = self.send.await_args.args
        self.assertEqual(channel, CHANNEL)
        self.assertIn("返事", text)
        self.assertIn(f"session_id={session_id}", text)
        self.assertTrue(all(bridge.peeks))  # 読むだけ。既読の印は、会話が自分で取りに来た時に付く
        self.assertEqual([(r["seq"], r["state"], r["conversation"]) for r in bridge.receipts],
                         [(2, "submitted", f"claude:{CHANNEL}"), (2, "started", f"claude:{CHANNEL}")])
        self.assertEqual(self.terminal.launched, [])
        self.close.assert_awaited_once_with(CHANNEL)  # 通話が終わったら、会話の待ち受けを残さない

    async def test_reply_the_conversation_already_fetched_is_not_put_in_again(self):
        session_id = self._call()
        bridge = Bridge([{"seq": 2, "message": "返事", "fetched": True}], status="hungup")
        await self._watch(session_id, bridge)
        self.assertEqual(self.send.await_count, 0)
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "fetched")])

    async def test_ended_conversation_hands_the_reply_to_the_folder_seat(self):
        session_id = self._call()
        self.send.side_effect = DeliveryError("CHANNEL_CLOSED", "channelは閉じています。本文は送っていません")
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge, receiver=True)
        self.assertEqual(len(self.terminal.launched), 1)
        harness, cwd, _seat, prompt = self.terminal.launched[0]
        self.assertEqual((harness, cwd), ("claude-code", str(self.folder)))
        self.assertIn("返事", prompt)
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "relaunched")])
        self.assertEqual(self.store.subscription(session_id)["delivery_mode"], "hosted")

    async def test_replies_nobody_takes_are_withdrawn_and_reach_the_seat_together(self):
        session_id = self._call()
        self.state.return_value = ("queued", False)  # 取り出す待ち受けが居ない。会話も生きていない
        bridge = Bridge([{"seq": 2, "message": "返事2"}, {"seq": 3, "message": "返事3"}], status="hungup")
        await self._watch(session_id, bridge)
        self.assertEqual((self.send.await_count, self.withdraw.await_count, len(self.terminal.launched)), (2, 2, 1))
        prompt = self.terminal.launched[0][3]
        self.assertLess(prompt.index("返事2"), prompt.index("返事3"))
        self.assertEqual(self._states(session_id), [(2, "relaunched"), (3, "relaunched")])
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts],
                         [(2, "submitted"), (3, "submitted"), (2, "relaunched"), (3, "relaunched")])

    async def test_busy_conversation_keeps_the_reply_until_its_turn_ends(self):
        session_id = self._call()
        answers = iter([("queued", True), ("queued", True), ("emitted", False)])  # 番の途中 → 番の終わりに取り出した
        self.state.side_effect = lambda *_a: next(answers)
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge)
        self.assertEqual((self.withdraw.await_count, self.terminal.launched), (0, []))
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "submitted"), (2, "started")])

    async def test_conversation_that_stays_silent_past_the_limit_gives_the_reply_to_the_seat(self):
        session_id = self._call()
        self.state.return_value = ("queued", True)  # process は居るが、待ち受けが切れたまま止まっている
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        with patch.object(local, "_CHANNEL_BUSY_SECONDS", 0.0):
            await self._watch(session_id, bridge)
        self.assertEqual((self.withdraw.await_count, len(self.terminal.launched)), (1, 1))
        self.assertEqual(bridge.receipts[-1]["detail"], "会話が返信を取り出さないまま止まっています")

    async def test_reply_taken_while_withdrawing_is_left_to_the_conversation(self):
        session_id = self._call()
        answers = iter([("queued", False), ("emitted", False)])
        self.state.side_effect = lambda *_a: next(answers)
        self.withdraw.return_value = False  # 取り下げる前に、待ち受けが取り出した
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge)
        self.assertEqual(self.terminal.launched, [])
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "submitted"), (2, "started")])

    async def test_unknown_delivery_stops_without_a_seat_or_a_second_send(self):
        session_id = self._call()
        self.state.return_value = ("unknown", False)
        bridge = Bridge([{"seq": 2, "message": "返事"}])
        await self._watch(session_id, bridge)
        self.assertEqual((self.send.await_count, self.withdraw.await_count, self.terminal.launched), (1, 0, []))
        self.assertEqual(self.store.subscription(session_id)["state"], "unknown")
        self.assertEqual(bridge.receipts[-1]["state"], "unknown")

    async def test_seat_that_fails_after_withdrawal_gets_the_reply_again_with_the_next_one(self):
        session_id = self._call()
        self.state.return_value = ("queued", False)
        self.terminal.launch_error = DeliveryError("AITERM_UNAVAILABLE", "Aiterm が見つかりません")
        bridge = Bridge([{"seq": 2, "message": "返事2"}])
        polls = {"n": 0}

        def next_reply():
            polls["n"] += 1
            if polls["n"] == 4:  # 席を立てられなかった後で、新しい返信が来る
                self.terminal.launch_error = None
                self.state.return_value = ("withdrawn", False)
                self.send.side_effect = DeliveryError("CHANNEL_DELIVERY_DUPLICATE", "同じ配送IDの本文はすでに受け取られています")
                bridge.messages.append({"seq": 3, "message": "返事3"})
                bridge.status = "hungup"

        bridge.on_poll = next_reply
        await self._watch(session_id, bridge)
        self.assertEqual(len(self.terminal.launched), 2)  # 1回目は失敗。2回目で立つ
        self.assertIn("返事2", self.terminal.launched[1][3])
        self.assertEqual(self.terminal.sent[-1][1].count("返事3"), 1)  # 続きの返信は、立った席へ
        self.assertEqual(self._states(session_id), [(2, "relaunched"), (3, "submitted")])
        self.assertEqual(self.withdraw.await_count, 1)  # 取り下げは1回だけ。会話へは入れ直さない


class CallOpenTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.store = local.LocalStore(Path(self.temp.name))
        self.session_id = str(uuid.uuid4())
        self.started: list[tuple[str, object]] = []
        for target, name, value in ((local, "store", self.store),
                                    (local, "_remote_tool", AsyncMock(return_value={"session_id": self.session_id})),
                                    (local, "_start_watch", lambda session, claimed=None: self.started.append((session, claimed))),
                                    (local, "_claim_session", lambda _session: None)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _ctx(self, client: str = "claude-code"):
        async def roots():
            return SimpleNamespace(roots=[SimpleNamespace(uri=Path(self.temp.name).as_uri())])
        return SimpleNamespace(
            session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=SimpleNamespace(name=client)), list_roots=roots),
            request_context=SimpleNamespace(meta=SimpleNamespace(model_extra={"claudecode/toolUseId": "toolu_1"})))

    async def test_claude_code_with_the_hook_gets_replies_by_itself(self):
        opened = AsyncMock(return_value={"channel_id": CHANNEL, "session_id": "conversation-1"})
        with patch.object(local.claude_channel, "open_channel", opened):
            result = await local.call_open("claude:x", "ベル", "リリーバイス", member_system="bellteam", ctx=self._ctx())
        opened.assert_awaited_once_with("claude-code", {"claudecode/toolUseId": "toolu_1"})
        self.assertEqual((result["parent_delivery"]["state"], result["parent_delivery"]["conversation"]),
                         ("watching", "conversation-1"))
        saved = self.store.subscription(self.session_id)
        self.assertEqual((saved["delivery_mode"], saved["thread_id"], saved["harness"], saved["cwd"]),
                         ("channel", CHANNEL, "claude-code", str(Path(self.temp.name))))

    async def test_claude_code_without_the_hook_keeps_fetching_by_itself(self):
        failed = AsyncMock(side_effect=DeliveryError("CLAUDE_PARENT_HOOK_UNAVAILABLE", "親のPreToolUse hookを確認できません"))
        with patch.object(local.claude_channel, "open_channel", failed):
            result = await local.call_open("claude:x", "ベル", "リリーバイス", member_system="bellteam", ctx=self._ctx())
        self.assertEqual(result["parent_delivery"]["state"], "manual")
        self.assertIn("CLAUDE_PARENT_HOOK_UNAVAILABLE", result["parent_delivery"]["reason"])
        self.assertEqual(self.store.subscription(self.session_id)["delivery_mode"], "manual")

    async def test_cursor_is_not_asked_for_a_channel(self):
        opened = AsyncMock()
        with patch.object(local.claude_channel, "open_channel", opened):
            result = await local.call_open("cursor:x", "ベル", "リリーバイス", member_system="bellteam",
                                           ctx=self._ctx("cursor-vscode"))
        self.assertEqual((opened.await_count, result["parent_delivery"]["state"]), (0, "manual"))
        self.assertNotIn("reason", result["parent_delivery"])

    async def test_new_claude_code_conversation_takes_the_call_over(self):
        self.store.add(self.session_id, "cb-claude-seat-12345678", Path(""), "リリーバイス", "hosted", "bellteam",
                       "claude-code", self.temp.name)
        opened = AsyncMock(return_value={"channel_id": CHANNEL, "session_id": "conversation-2"})
        with patch.object(local.claude_channel, "open_channel", opened):
            result = await local.call_adopt(self.session_id, ctx=self._ctx())
        self.assertEqual((result["previous"]["delivery_mode"], result["parent_delivery"]["state"]), ("hosted", "watching"))
        saved = self.store.subscription(self.session_id)
        self.assertEqual((saved["delivery_mode"], saved["thread_id"]), ("channel", CHANNEL))
        failed = AsyncMock(side_effect=DeliveryError("CLAUDE_PARENT_HOOK_UNAVAILABLE", "hook がありません"))
        with patch.object(local.claude_channel, "open_channel", failed), self.assertRaises(DeliveryError) as caught:
            await local.call_adopt(self.session_id, ctx=self._ctx())
        self.assertEqual(caught.exception.code, "PARENT_UNSUPPORTED")


class HarnessHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.tmp.name, "CALL_BRIDGE_TOKEN": "secret-token"})
        env.start()
        self.addCleanup(env.stop)
        self.registered = False

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[2] in ("add", "remove"):
            self.registered = command[2] == "add"
        shown = "call-bridge: python -m call_bridge.local" if self.registered and command[2] == "get" else ""
        return subprocess.CompletedProcess(command, 0 if command[2] != "get" or self.registered else 1, shown, "")

    def test_enable_registers_the_hook_and_status_tells_how_replies_arrive(self) -> None:
        actions: list[str] = []

        def installed(_name):
            directory = Path(self.tmp.name) / "steer"
            directory.mkdir()
            (directory / claude_channel.CLI).write_text("", encoding="utf-8")
            return {"library": "x", "version": "0.4.3"}

        def hooks(action):
            actions.append(action)
            return {"ok": True, "registered": True, "missing": []}

        with patch.object(harness_setup.shutil, "which", side_effect=lambda name: f"/bin/{name}"), \
             patch.object(claude_channel, "install", installed), patch.object(claude_channel, "hooks", hooks):
            self.assertEqual(harness_setup.enable("claude-code", self._run),
                             {"harness": "claude-code", "status": "registered", "delivery": "automatic"})
            self.assertEqual(actions, ["enable", "status"])
            harness_setup.disable("claude-code", self._run)
            self.assertEqual(actions[2], "disable")

    def test_a_terminal_without_the_package_is_still_registered_and_says_why(self) -> None:
        missing = DeliveryError("STEER_DELIVERY_UNAVAILABLE", "aiterm-steer-delivery が見つかりません")
        with patch.object(harness_setup.shutil, "which", side_effect=lambda name: f"/bin/{name}"), \
             patch.object(claude_channel, "install", side_effect=missing):
            result = harness_setup.enable("claude-code", self._run)
        self.assertEqual((result["status"], result["delivery"]), ("registered", "manual"))
        self.assertIn("STEER_DELIVERY_UNAVAILABLE", result["warning"])


class NodePathTest(unittest.TestCase):
    """控えた Node の場所は、Node を上げると無くなる事がある（Homebrew の版つきの実体など）。"""

    def test_node_that_disappeared_is_found_again_on_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CALL_BRIDGE_STATE": tmp}):
            steer = Path(tmp) / "steer"
            steer.mkdir()
            (steer / claude_channel.CLI).write_text("", encoding="utf-8")
            (steer / "steer.json").write_text(json.dumps({"node": str(Path(tmp) / "gone" / "node")}), encoding="utf-8")
            with patch.object(claude_channel.shutil, "which", return_value=sys.executable):
                self.assertEqual(claude_channel._command(), [sys.executable, str(steer / claude_channel.CLI)])
            with patch.object(claude_channel.shutil, "which", return_value=None), self.assertRaises(DeliveryError) as caught:
                claude_channel._command()
            self.assertEqual(caught.exception.code, "CLAUDE_CHANNEL_UNAVAILABLE")

    def test_codex_delivery_finds_node_again_too(self) -> None:
        from call_bridge import codex_delivery
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "cli.js"
            cli.write_text("", encoding="utf-8")
            config = {"steer_cli": str(cli), "steer_node": str(Path(tmp) / "gone" / "node")}
            with patch.dict(os.environ, {"AITERM_STEER_DELIVERY": ""}), \
                 patch.object(codex_delivery.shutil, "which", return_value=sys.executable):
                command, _env = codex_delivery._steer_command(config)
            self.assertEqual(command, [sys.executable, str(cli.resolve())])


def _steer_cli() -> Path | None:
    """本物の aiterm-steer-delivery（0.4.2 以降）の dist/cli.js。無ければ、Node を通す試験は飛ばす。"""
    named = os.environ.get("CALL_BRIDGE_TEST_STEER_CLI")
    found = Path(named) if named else None
    if found is None:
        binary = shutil.which("aiterm-steer-delivery")
        found = Path(binary).resolve() if binary else None
    if found is None or not shutil.which("node") or not (found.parent / "index.js").is_file():
        return None
    version = claude_channel._version(found.parent / "index.js")
    return found if version is not None and version >= claude_channel.MIN_VERSION else None


@unittest.skipUnless(_steer_cli() and os.name != "nt", "aiterm-steer-delivery 0.4.2 以降と Node が要ります")
class NodeEntryTest(unittest.IsolatedAsyncioTestCase):
    """本物のパッケージを通す。この process が Claude Code の代わりに hook を起こす。"""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Path(self.temp.name) / "claude" / "settings.json"
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "echo other"}]}]}}), encoding="utf-8")
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": str(Path(self.temp.name) / "state"),
                                      "AITERM_STEER_DELIVERY": str(_steer_cli())})
        env.start()
        self.addCleanup(env.stop)
        patcher = patch.object(claude_channel, "settings_file", lambda: self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        claude_channel.install("call-bridge")
        self.session, self.tool = str(uuid.uuid4()), "toolu_test01"

    def _hook(self, event: dict) -> subprocess.Popen:
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        command = document["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        process = subprocess.Popen(["sh", "-c", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        process.stdin.write(json.dumps({"session_id": self.session, **event}))
        process.stdin.close()
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait(),
                                 process.stdout.close(), process.stderr.close()))
        return process

    async def test_hook_registration_keeps_other_hooks_and_can_be_removed(self):
        self.assertEqual(claude_channel.hooks("status")["registered"], False)
        self.assertEqual(claude_channel.hooks("enable")["result"], "configured")
        self.assertEqual(claude_channel.hooks("enable")["result"], "unchanged")
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(document["model"], "x")
        self.assertEqual(document["hooks"]["Stop"][0]["hooks"][0]["command"], "echo other")
        self.assertEqual(document["hooks"]["PreToolUse"][0]["matcher"], "^mcp__call-bridge__(call_open|call_adopt)$")
        found = claude_channel.hooks("status")
        self.assertEqual((found["registered"], found["missing"]), (True, []))
        self.assertTrue(all(Path(script).name == claude_channel.HOOK for script in found["scripts"]))
        self.assertEqual(claude_channel.hooks("disable")["result"], "removed")
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(document["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": "echo other"}]}])
        self.assertNotIn(claude_channel.HOOK, json.dumps(document))

    async def test_reply_wakes_the_conversation_and_leftovers_can_be_withdrawn(self):
        claude_channel.hooks("enable")
        with self.assertRaises(DeliveryError) as caught:  # hook が道具の呼び出しを見ていない会話は、見分けられない
            await claude_channel.open_channel("claude-code", {"claudecode/toolUseId": "toolu_none"})
        self.assertIn(caught.exception.code, claude_channel.UNAVAILABLE)
        self.assertEqual(self._hook({"hook_event_name": "PreToolUse", "tool_use_id": self.tool,
                                     "tool_name": "mcp__call-bridge__call_open"}).wait(30), 0)
        opened = await claude_channel.open_channel("claude-code", {"claudecode/toolUseId": self.tool})
        self.assertEqual(opened["session_id"], self.session)
        channel = opened["channel_id"]

        waiter = self._hook({"hook_event_name": "PostToolUse", "tool_use_id": self.tool,
                             "tool_name": "mcp__call-bridge__call_open"})
        first = str(uuid.uuid4())
        await claude_channel.send(channel, first, "返信その1\n二行目")
        self.assertEqual(waiter.wait(30), 2)  # 2 で終わると、Claude Code は止まっている会話を起こす
        self.assertEqual(waiter.stderr.read(), "返信その1\n二行目")
        self.assertEqual(await claude_channel.delivery_state(channel, first), ("emitted", False))

        second = str(uuid.uuid4())
        await claude_channel.send(channel, second, "返信その2")  # 番の途中。待ち受けは居ない
        self.assertEqual(await claude_channel.delivery_state(channel, second), ("queued", True))  # この process は生きている
        stop = self._hook({"hook_event_name": "Stop"})
        self.assertEqual((stop.wait(30), stop.stderr.read()), (2, "返信その2"))

        third = str(uuid.uuid4())
        await claude_channel.send(channel, third, "返信その3")
        self.assertTrue(await claude_channel.withdraw(channel, third))
        self.assertEqual((await claude_channel.delivery_state(channel, third))[0], "withdrawn")
        self.assertFalse(await claude_channel.withdraw(channel, first))  # 会話へ出た物は取り下げられない
        with self.assertRaises(DeliveryError) as caught:
            await claude_channel.send(channel, third, "返信その3")
        self.assertEqual(caught.exception.code, "CHANNEL_DELIVERY_DUPLICATE")

        self.assertEqual(self._hook({"hook_event_name": "SessionEnd"}).wait(30), 0)
        with self.assertRaises(DeliveryError) as caught:
            await claude_channel.send(channel, str(uuid.uuid4()), "返信その4")
        self.assertEqual((caught.exception.code, caught.exception.outcome_unknown), ("CHANNEL_CLOSED", False))


if __name__ == "__main__":
    unittest.main()
