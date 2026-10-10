"""通話を結び付けた会話が止まっている時の扱い。会話の状態の読み取りから、席への付け替えまで。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from call_bridge import aiterm, conversation, local, relaunch
from call_bridge.codex_delivery import DeliveryError


def _event(kind: str) -> str:
    return json.dumps({"type": "event_msg", "payload": {"type": kind}})


class TurnTailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "rollout.jsonl"

    def test_last_turn_boundary_decides_the_state(self) -> None:
        for rows, expected in (([], "none"), ([_event("task_started")], "running"),
                               ([_event("task_started"), _event("task_complete")], "completed"),
                               ([_event("task_complete"), _event("task_started"), _event("turn_aborted")], "interrupted")):
            self.file.write_text("\n".join(rows) + "\n", encoding="utf-8")
            self.assertEqual(conversation.read_turn_tail(self.file), expected)

    def test_text_that_only_mentions_a_boundary_is_not_a_boundary(self) -> None:
        said = json.dumps({"type": "response_item", "payload": {"type": "message", "text": "turn_aborted と書いた"}})
        self.file.write_text("\n".join([_event("task_complete"), said, "{壊れた行 task_started"]) + "\n", encoding="utf-8")
        self.assertEqual(conversation.read_turn_tail(self.file), "completed")

    def test_boundary_far_from_the_end_is_found_and_a_missing_file_is_unknown(self) -> None:
        filler = json.dumps({"type": "response_item", "payload": {"text": "x" * 4000}})
        self.file.write_text("\n".join([_event("turn_aborted")] + [filler] * 200) + "\n", encoding="utf-8")
        self.assertGreater(self.file.stat().st_size, 256 * 1024)
        self.assertEqual(conversation.read_turn_tail(self.file), "interrupted")
        self.assertEqual(conversation.read_turn_tail(Path(self.tmp.name) / "none.jsonl"), "unknown")


class ConversationTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_conversation_and_a_read_conversation(self) -> None:
        async def gone(_method, _params):
            raise DeliveryError("CODEX_REQUEST_REJECTED", "{'code': -32600, 'message': 'thread not loaded: t-1'}")

        async def broken(_method, _params):
            raise DeliveryError("CODEX_REQUEST_TIMEOUT", "thread/read が時間内に返りませんでした")

        from call_bridge.codex_delivery import conversation_missing
        self.assertFalse((await conversation.read_conversation(gone, "t-1", conversation_missing)).exists)
        with self.assertRaises(DeliveryError):  # 時間切れは「無い」とは決めない
            await conversation.read_conversation(broken, "t-1", conversation_missing)

        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "rollout.jsonl"
            record.write_text(_event("turn_aborted") + "\n", encoding="utf-8")
            os.utime(record, (time.time() - 600, time.time() - 600))

            async def found(_method, _params):
                return {"thread": {"id": "t-1", "source": "vscode", "cwd": "/work", "path": str(record)}}

            seen = await conversation.read_conversation(found, "t-1", conversation_missing)
        self.assertEqual((seen.exists, seen.app, seen.cwd, seen.turn), (True, True, "/work", "interrupted"))
        self.assertGreater(seen.idle_seconds, 500)

    async def test_withdraw_confirms_the_queue_no_longer_holds_the_reply(self) -> None:
        queue = [{"id": "q-1", "clientUserMessageId": "d-1"}, {"id": "q-2", "clientUserMessageId": "d-2"}]
        calls: list[tuple[str, dict]] = []

        async def request(method, params):
            calls.append((method, params))
            if method == "thread/queue/list":
                return {"data": list(queue), "nextCursor": None}
            removed = [row for row in queue if row["id"] == params["queuedSubmissionId"]]
            for row in removed:
                queue.remove(row)
            return {"deleted": bool(removed)}

        self.assertEqual(await conversation.queued_entry(request, "t-1", "d-2"), "q-2")
        self.assertTrue(await conversation.withdraw(request, "t-1", "d-2"))
        self.assertIn(("thread/queue/delete", {"threadId": "t-1", "queuedSubmissionId": "q-2"}), calls)
        self.assertFalse(await conversation.withdraw(request, "t-1", "d-2"))  # もう無い物は、取り消したとは言わない
        self.assertEqual([row["id"] for row in queue], ["q-1"])

    async def test_successor_follows_finished_handoffs_only(self) -> None:
        operations = [
            {"source_thread_id": "a", "target_thread_id": "b", "state": "continued"},
            {"source_thread_id": "b", "target_thread_id": "c", "state": "continued"},
            {"source_thread_id": "c", "target_thread_id": None, "state": "failed"},
            {"source_thread_id": "x", "target_thread_id": "y", "state": "failed"},
        ]
        self.assertEqual(conversation.successor("a", operations), "c")
        self.assertIsNone(conversation.successor("c", operations))
        self.assertIsNone(conversation.successor("x", operations))
        loop = [{"source_thread_id": "p", "target_thread_id": "q", "state": "continued"},
                {"source_thread_id": "q", "target_thread_id": "p", "state": "continued"}]
        self.assertEqual(conversation.successor("p", loop), "q")

    async def test_handoff_state_uses_the_successor_command_and_falls_back_to_the_list(self) -> None:
        def answer(value):
            async def run(args):
                return value if args[1] == "successor" else {"operations": [
                    {"source_thread_id": "a", "target_thread_id": "old-list", "state": "continued"}]}
            return run

        schema = "throughline.codex_auto_handoff_successor.v1"
        cases = (
            ({"schema": schema, "thread_id": "a", "current_thread_id": "c", "pending": None}, conversation.Handoff(current="c")),
            ({"schema": schema, "thread_id": "a", "current_thread_id": "a", "pending": None}, conversation.Handoff()),
            ({"schema": schema, "thread_id": "a", "current_thread_id": "a",
              "pending": {"handoff_id": "h-1", "in_flight": True}}, conversation.Handoff(in_flight=True)),
            ({"schema": schema, "thread_id": "a", "current_thread_id": "a",
              "pending": {"handoff_id": "h-1", "in_flight": False, "error_code": "x"}},
             conversation.Handoff(stopped="handoff_id=h-1 error_code=x")),
            (None, conversation.Handoff(current="old-list")),  # successor の無い版
        )
        for value, expected in cases:
            with patch.object(conversation, "_throughline_json", answer(value)):
                self.assertEqual(await conversation.handoff_state("a"), expected)

    async def test_handoff_operations_reads_the_public_command_and_survives_its_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "throughline"
            script.write_text(f"#!{sys.executable}\nimport json, sys\nassert sys.argv[1:] == ['auto-handoff', 'status', '--json']\n"
                              "print(json.dumps({'operations': [{'source_thread_id': 'a', 'target_thread_id': 'b', "
                              "'state': 'continued'}, 'x']}))\n", encoding="utf-8")
            script.chmod(0o700)
            with patch.dict(os.environ, {"CALL_BRIDGE_THROUGHLINE": str(script)}):
                self.assertEqual(await conversation.handoff_operations(),
                                 [{"source_thread_id": "a", "target_thread_id": "b", "state": "continued"}])
            with patch.dict(os.environ, {"CALL_BRIDGE_THROUGHLINE": str(Path(tmp) / "missing")}):
                self.assertEqual(await conversation.handoff_operations(), [])


class FakeTerminal:
    """Aiterm の代わり。動いている席と、送った文・立てた席を覚える。"""

    def __init__(self, seats: dict[str, str | None] | None = None):
        self.seats = dict(seats or {})
        self.sent: list[tuple[str, str]] = []
        self.launched: list[tuple[str, str, str, str]] = []
        self.closed: list[str] = []
        self.launch_error: Exception | None = None
        self.prompt_delivered = True

    async def sessions(self):
        return dict(self.seats)

    async def send(self, session_id, text):
        self.sent.append((session_id, text))

    async def launch(self, harness, cwd, session_name, prompt):
        self.launched.append((harness, cwd, session_name, prompt))
        if self.launch_error is not None:
            raise self.launch_error
        self.seats[session_name] = aiterm.HARNESSES[harness]
        return session_name, self.prompt_delivered

    async def close(self, session_id):
        self.closed.append(session_id)

    def connect(self):
        @asynccontextmanager
        async def opened():
            yield self
        return opened()


class Bridge:
    """通話のサーバーの代わり。決めた返信を返し、端末の側の知らせを覚える。"""

    def __init__(self, messages: list[dict], status: str = "open"):
        self.messages, self.status = messages, status
        self.receipts: list[dict] = []
        self.peeks: list[bool] = []
        self.on_poll = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def get(self, url, params, headers=None):
        self.peeks.append(params.get("peek") == "1")
        if self.on_poll is not None:
            self.on_poll()
        rows = [row for row in self.messages if row["seq"] > params["after_seq"]]
        return httpx.Response(200, json={"ok": True, "status": self.status, "messages": rows},
                              request=httpx.Request("GET", url))

    async def post(self, url, headers=None, json=None):
        self.receipts.append(json)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))


def _conversation(turn: str, *, exists: bool = True, idle: float = 3600.0, cwd: str | None = None):
    return conversation.Conversation("thread", exists, "vscode" if exists else None, cwd, turn, idle)


class StoppedConversationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "approval-box"
        self.folder.mkdir()
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.temp.name, "CALL_BRIDGE_TOKEN": "test-token",
                                      "CALL_BRIDGE_MCP_URL": "http://localhost:18910/mcp"})
        env.start()
        self.addCleanup(env.stop)
        self.store = local.LocalStore(Path(self.temp.name))
        self.terminal = FakeTerminal()
        self.submit = AsyncMock(return_value="queue-id")
        for target, name, value in ((local, "submit_reply", self.submit), (local, "_POLL_SECONDS", 0),
                                    (local, "_CONFIRM_AFTER_SECONDS", 0.0), (local, "_RECHECK_SECONDS", 0.0),
                                    (local.aiterm, "connect", self.terminal.connect),
                                    (relaunch.aiterm, "connect", self.terminal.connect)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, thread_id: str, member: str = "ナユタ", **more) -> str:
        session_id = str(uuid.uuid4())
        self.store.add(session_id, thread_id, Path(self.temp.name), member, **more)
        return session_id

    async def _watch(self, session_id: str, bridge: Bridge, *, receiver: bool = False, **patches) -> None:
        defaults = {"codex_conversation": AsyncMock(return_value=_conversation("completed")),
                    "codex_queued": AsyncMock(return_value=(False, _conversation("completed"))),
                    "handoff_operations": AsyncMock(return_value=[])}
        defaults.update(patches)
        operations = defaults.pop("handoff_operations")
        handoff = defaults.pop("handoff_state", None)

        async def from_operations(thread_id):
            return conversation.Handoff(current=conversation.successor(thread_id, await operations()))

        with patch.object(local.httpx, "AsyncClient", return_value=bridge), \
             patch.object(local.talk, "handoff_state", handoff or from_operations):
            for name, value in defaults.items():
                patcher = patch.object(local, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            await asyncio.wait_for(local.Watchers(self.store, receiver=receiver).watch(session_id), 5)

    async def test_healthy_conversation_gets_the_reply_and_the_sender_can_see_it_started(self):
        session_id = self._call("thread-a", cwd=str(self.folder))
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge)
        self.assertEqual(self.submit.await_count, 1)
        self.assertEqual([(r["seq"], r["state"], r["conversation"]) for r in bridge.receipts],
                         [(2, "submitted", "codex:thread-a"), (2, "started", "codex:thread-a")])
        self.assertEqual(self.terminal.launched, [])
        self.assertEqual(self.store.status(session_id)["state"], "closed")

    async def test_interrupted_conversation_with_a_handoff_is_followed_with_every_call_bound_to_it(self):
        first = self._call("thread-a", cwd=str(self.folder))
        second = self._call("thread-a", "フラジャイル", cwd=str(self.folder))
        other = self._call("thread-z", cwd=str(self.folder))
        states = {"thread-a": _conversation("interrupted"), "thread-b": _conversation("completed")}
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(first, bridge,
                          codex_conversation=AsyncMock(side_effect=lambda thread, _home: states[thread]),
                          handoff_operations=AsyncMock(return_value=[
                              {"source_thread_id": "thread-a", "target_thread_id": "thread-b", "state": "continued"}]))
        self.assertEqual(self.submit.await_args.args[0], "thread-b")
        self.assertEqual(self.store.subscription(second)["thread_id"], "thread-b")
        self.assertEqual(self.store.subscription(other)["thread_id"], "thread-z")
        self.assertEqual(self.terminal.launched, [])
        self.assertEqual(bridge.receipts[0]["conversation"], "codex:thread-b")

    async def test_interrupted_conversation_without_a_handoff_goes_to_the_folder_seat(self):
        first = self._call("thread-a", cwd=str(self.folder))
        second = self._call("thread-a", "フラジャイル", cwd=str(self.folder))
        bridge = Bridge([{"seq": 2, "message": "一通目"}, {"seq": 4, "message": "二通目"}], status="hungup")
        await self._watch(first, bridge, codex_conversation=AsyncMock(return_value=_conversation("interrupted")))
        seat = relaunch.seat_name("codex", str(self.folder))
        self.assertEqual(self.submit.await_count, 0)  # 止まった会話のキューへは入れない
        self.assertEqual([(row[0], row[1], row[2]) for row in self.terminal.launched], [("codex", str(self.folder), seat)])
        prompt = self.terminal.launched[0][3]
        self.assertIn("一通目", prompt)
        self.assertIn(f"session_id={first} 相手=ナユタ", prompt)
        self.assertIn(f"session_id={second} 相手=フラジャイル", prompt)
        # 2通目は、立てた席へ送る。席は1つだけ。
        self.assertEqual([seat for seat, _text in self.terminal.sent], [seat])
        self.assertIn("二通目", self.terminal.sent[0][1])
        for session_id in (first, second):
            bound = self.store.subscription(session_id)
            self.assertEqual((bound["delivery_mode"], bound["thread_id"]), ("hosted", seat))
        self.assertEqual([(r["seq"], r["state"], r["conversation"]) for r in bridge.receipts],
                         [(2, "relaunched", f"hosted:{seat}"), (4, "submitted", f"hosted:{seat}")])
        self.assertEqual([row["state"] for row in self.store.relaunches()], ["started", "starting"])

    async def test_a_conversation_interrupted_just_now_is_left_for_its_handoff(self):
        session_id = self._call("thread-a", cwd=str(self.folder))
        polls = {"n": 0}
        bridge = Bridge([{"seq": 2, "message": "返事"}])

        def count():
            polls["n"] += 1
            if polls["n"] >= 3:
                self.store.stop(session_id, "closed")
        bridge.on_poll = count
        await self._watch(session_id, bridge,
                          codex_conversation=AsyncMock(return_value=_conversation("interrupted", idle=5.0)))
        self.assertEqual((self.submit.await_count, self.terminal.launched, bridge.receipts), (0, [], []))
        self.assertEqual(self.store.status(session_id)["deliveries"][0]["state"], "waiting")

    async def test_handoff_still_being_made_is_waited_for_and_a_stopped_one_is_reported_not_replaced(self):
        session_id = self._call("thread-a", cwd=str(self.folder))
        answers = [conversation.Handoff(in_flight=True), conversation.Handoff(in_flight=True),
                   conversation.Handoff(stopped="handoff_id=h-1 error_code=handoff_thread_mismatch")]
        asked = AsyncMock(side_effect=lambda _thread: answers.pop(0) if len(answers) > 1 else answers[0])
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge, handoff_state=asked,
                          codex_conversation=AsyncMock(return_value=_conversation("interrupted")))
        self.assertEqual((self.submit.await_count, self.terminal.launched), (0, []))  # 席は立てない
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "failed")])
        self.assertIn("handoff_id=h-1 error_code=handoff_thread_mismatch", bridge.receipts[0]["detail"])
        self.assertEqual(asked.await_count, 3)

    async def test_missing_conversation_uses_the_folder_recorded_when_the_call_was_opened(self):
        session_id = self._call("thread-a", cwd=str(self.folder))
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge,
                          codex_conversation=AsyncMock(return_value=_conversation("unknown", exists=False)))
        self.assertEqual(len(self.terminal.launched), 1)
        self.assertEqual(bridge.receipts[0]["state"], "relaunched")
        self.assertIn("会話が見つかりません", bridge.receipts[0]["detail"])

    async def test_reply_that_cannot_be_handed_over_is_held_until_a_newer_reply_arrives(self):
        session_id = self._call("thread-a", cwd=str(self.folder))
        seat = relaunch.seat_name("codex", str(self.folder))
        # 席が起動の画面で止まった（認証切れなど）。最初の文は入っていない。
        self.terminal.launch_error = aiterm.AitermError("AITERM_LAUNCH_FAILED", "startup_dialog", sent=False,
                                                        launch={"session_id": seat})
        bridge = Bridge([{"seq": 2, "message": "一通目"}])
        polls = {"n": 0}

        def later():
            polls["n"] += 1
            if polls["n"] == 4:
                self.terminal.launch_error = None
                bridge.messages.append({"seq": 4, "message": "二通目"})
                bridge.status = "hungup"
        bridge.on_poll = later
        gone = AsyncMock(return_value=_conversation("unknown", exists=False))
        await self._watch(session_id, bridge, codex_conversation=gone)
        self.assertEqual(gone.await_count, 2)  # 同じ返信を、取りに行くたびに試し直さない
        self.assertEqual(self.terminal.closed, [seat])  # 残った端末は閉じてある
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts],
                         [(2, "failed"), (2, "relaunched"), (4, "submitted")])
        self.assertIn("一通目", self.terminal.launched[1][3])
        self.assertIn("二通目", self.terminal.sent[0][1])
        self.assertEqual(self.store.status(session_id)["state"], "closed")

    async def test_call_bound_before_this_version_has_no_folder_and_says_so(self):
        session_id = self._call("thread-a")  # 前の版が作った結び付け：フォルダの控えが無い
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge,
                          codex_conversation=AsyncMock(return_value=_conversation("unknown", exists=False)))
        self.assertEqual(self.terminal.launched, [])
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "failed")])
        self.assertIn("RELAUNCH_FOLDER_UNKNOWN", bridge.receipts[0]["detail"])
        # 会話が残っていれば、フォルダは会話から読める。
        other = self._call("thread-b")
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(other, bridge, codex_conversation=AsyncMock(
            return_value=_conversation("interrupted", cwd=str(self.folder))))
        self.assertEqual(len(self.terminal.launched), 1)
        self.assertEqual(self.store.subscription(other)["cwd"], str(self.folder))

    async def test_reply_left_in_a_sleeping_queue_is_withdrawn_before_the_seat_gets_it(self):
        session_id = self._call("thread-a", cwd=str(self.folder))
        asleep = _conversation("completed")
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        withdraw = AsyncMock(return_value="withdrawn")
        await self._watch(session_id, bridge, codex_queued=AsyncMock(return_value=(True, asleep)),
                          codex_withdraw=withdraw, fetch_body=AsyncMock(return_value="返事"))
        self.assertEqual((self.submit.await_count, withdraw.await_count, len(self.terminal.launched)), (1, 1, 1))
        self.assertIn("返事", self.terminal.launched[0][3])
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "submitted"), (2, "relaunched")])
        self.assertEqual(self.store.status(session_id)["deliveries"][0]["state"], "relaunched")

    async def test_reply_in_a_running_conversation_is_left_to_its_queue(self):
        session_id = self._call("thread-a", cwd=str(self.folder))
        running = _conversation("running")
        answers = iter([(True, running), (True, running), (False, _conversation("completed"))])
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        withdraw = AsyncMock()
        await self._watch(session_id, bridge, codex_queued=AsyncMock(side_effect=lambda *_a: next(answers)),
                          codex_withdraw=withdraw)
        self.assertEqual((withdraw.await_count, self.terminal.launched), (0, []))
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "submitted"), (2, "started")])

    async def test_withdrawal_that_cannot_be_confirmed_stops_without_a_second_delivery(self):
        session_id = self._call("thread-a", cwd=str(self.folder))
        bridge = Bridge([{"seq": 2, "message": "返事"}], status="hungup")
        await self._watch(session_id, bridge, codex_queued=AsyncMock(return_value=(True, _conversation("completed"))),
                          codex_withdraw=AsyncMock(return_value="stuck"))
        self.assertEqual(self.terminal.launched, [])
        self.assertEqual(self.store.status(session_id)["state"], "unknown")
        self.assertEqual(bridge.receipts[-1]["state"], "unknown")

    async def test_seat_that_is_running_gets_the_reply_and_a_closed_seat_is_started_again(self):
        seat = relaunch.seat_name("claude-code", str(self.folder))
        session_id = self._call(seat, delivery_mode="hosted", harness="claude-code", cwd=str(self.folder))
        self.terminal.seats[seat] = "claude-code"
        bridge = Bridge([{"seq": 2, "message": "一通目"}], status="hungup")
        await self._watch(session_id, bridge)
        self.assertEqual(([s for s, _t in self.terminal.sent], self.terminal.launched), ([seat], []))

        closed = self._call(seat, delivery_mode="hosted", harness="claude-code", cwd=str(self.folder))
        self.terminal.seats.clear()
        bridge = Bridge([{"seq": 2, "message": "二通目"}], status="hungup")
        await self._watch(closed, bridge)
        self.assertEqual([(row[0], row[2]) for row in self.terminal.launched], [("claude-code", seat)])
        self.assertEqual(bridge.receipts[0]["state"], "relaunched")

    async def test_conversation_that_fetches_by_itself_is_left_alone_while_it_lives(self):
        session_id = self._call("", delivery_mode="manual", harness="grok", cwd=str(self.folder))
        bridge = Bridge([{"seq": 2, "message": "返事", "fetched": False}])
        polls = {"n": 0}

        def fetch():
            polls["n"] += 1
            if polls["n"] == 3:
                bridge.messages[0]["fetched"] = True
                bridge.status = "hungup"
        bridge.on_poll = fetch
        await self._watch(session_id, bridge)  # 通話を開いた会話の MCP
        self.assertTrue(all(bridge.peeks))  # 会話の代わりに既読を付けない
        self.assertEqual(self.terminal.launched, [])
        self.assertEqual([(r["seq"], r["state"]) for r in bridge.receipts], [(2, "fetched")])

    async def test_receiver_hands_an_unfetched_reply_to_the_folder_seat_once_the_conversation_is_gone(self):
        session_id = self._call("", delivery_mode="manual", harness="cursor", cwd=str(self.folder))
        bridge = Bridge([{"seq": 2, "message": "返事", "fetched": False}], status="hungup")
        await self._watch(session_id, bridge, receiver=True)
        seat = relaunch.seat_name("cursor", str(self.folder))
        self.assertEqual([(row[0], row[2]) for row in self.terminal.launched], [("cursor", seat)])
        self.assertEqual(bridge.receipts[0]["state"], "relaunched")
        self.assertIn("通話を開いた会話が終わっていました", bridge.receipts[0]["detail"])
        self.assertEqual(self.store.subscription(session_id)["delivery_mode"], "hosted")

    async def test_receiver_cannot_take_a_call_whose_conversation_still_holds_it(self):
        session_id = self._call("", delivery_mode="manual", harness="claude-code", cwd=str(self.folder))
        held = local._claim_session(session_id)
        self.addCleanup(os.close, held)
        self.assertIsNone(local._claim_session(session_id))


class HandOverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.store = local.LocalStore(Path(self.temp.name))
        self.folder = Path(self.temp.name) / "work"
        self.folder.mkdir()
        self.session_id = str(uuid.uuid4())
        self.store.add(self.session_id, "thread-a", Path(self.temp.name), "ナユタ", cwd=str(self.folder))
        self.seat = relaunch.seat_name("codex", str(self.folder))

    def _subscription(self) -> dict:
        return self.store.subscription(self.session_id)

    def test_seat_name_is_one_per_harness_and_folder_and_safe_as_a_session_name(self):
        other = Path(self.temp.name) / "別の 作業"
        self.assertEqual(self.seat, relaunch.seat_name("codex", str(self.folder) + os.sep))
        self.assertNotEqual(self.seat, relaunch.seat_name("claude-code", str(self.folder)))
        self.assertNotEqual(self.seat, relaunch.seat_name("codex", str(other)))
        for name in (self.seat, relaunch.seat_name("grok", str(other)), relaunch.seat_name("cursor", "/x/" + "a" * 90)):
            self.assertRegex(name, r"^[A-Za-z0-9_-]{1,64}$")

    async def test_seat_started_without_its_first_prompt_gets_the_prompt_by_send(self):
        terminal = FakeTerminal()
        terminal.prompt_delivered = False
        await relaunch.hand_over(self.store, self._subscription(), ["本文"], "理由", terminal.connect)
        self.assertEqual(len(terminal.launched), 1)
        self.assertEqual([seat for seat, _text in terminal.sent], [self.seat])

    async def test_launch_that_stopped_at_a_startup_screen_closes_the_leftover_and_binds_nothing(self):
        terminal = FakeTerminal()
        terminal.launch_error = aiterm.AitermError("AITERM_LAUNCH_FAILED", "startup_dialog", sent=False,
                                                   launch={"session_id": self.seat})
        with self.assertRaises(aiterm.AitermError) as caught:
            await relaunch.hand_over(self.store, self._subscription(), ["本文"], "理由", terminal.connect)
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(terminal.closed, [self.seat])
        self.assertEqual(self._subscription()["delivery_mode"], "queue")
        self.assertEqual(self.store.relaunches()[0]["state"], "failed")

    async def test_launch_with_an_unknown_result_is_not_started_again_and_nothing_is_closed(self):
        terminal = FakeTerminal()
        terminal.launch_error = aiterm.AitermError("AITERM_LAUNCH_FAILED", "応答なし", sent=None)
        with self.assertRaises(aiterm.AitermError) as caught:
            await relaunch.hand_over(self.store, self._subscription(), ["本文"], "理由", terminal.connect)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual((terminal.closed, self.store.relaunches()[0]["state"]), ([], "unknown"))

    async def test_a_name_used_by_another_terminal_and_an_unknown_folder_are_refused_before_anything_is_sent(self):
        terminal = FakeTerminal({self.seat: None})
        with self.assertRaises(DeliveryError) as taken:
            await relaunch.hand_over(self.store, self._subscription(), ["本文"], "理由", terminal.connect)
        self.assertEqual((taken.exception.code, terminal.sent, terminal.launched), ("RELAUNCH_NAME_TAKEN", [], []))
        with self.assertRaises(DeliveryError) as folder:
            await relaunch.hand_over(self.store, {**self._subscription(), "cwd": str(self.folder / "gone")}, ["本文"],
                                     "理由", terminal.connect)
        self.assertEqual(folder.exception.code, "RELAUNCH_FOLDER_UNKNOWN")

    async def test_two_watchers_do_not_start_the_same_seat_at_once(self):
        terminal = FakeTerminal()
        with relaunch.seat_lock(self.seat):
            with self.assertRaises(DeliveryError) as busy:
                await relaunch.hand_over(self.store, self._subscription(), ["本文"], "理由", terminal.connect)
        self.assertEqual((busy.exception.code, busy.exception.outcome_unknown, terminal.launched),
                         ("RELAUNCH_BUSY", False, []))
        await relaunch.hand_over(self.store, self._subscription(), ["本文"], "理由", terminal.connect)
        self.assertEqual(len(terminal.launched), 1)


if __name__ == "__main__":
    unittest.main()
