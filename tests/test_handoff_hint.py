"""Throughline の後継へ知らせる「前の会話が持っていた通話」。"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from call_bridge import conversation, handoff_hint, local
from call_bridge.codex_delivery import DeliveryError


class HandoffHintTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.store = local.LocalStore(Path(self.temp.name))
        self.calls = {name: str(uuid.uuid4()) for name in ("mine", "paused", "other", "seat", "closed")}
        self.store.add(self.calls["mine"], "channel-1", Path(""), "リリーバイス", "channel", "bellteam", "claude-code", "/work")
        self.store.add(self.calls["paused"], "channel-2", Path(""), "エレグ", "channel", "bellteam", "claude-code", "/work")
        self.store.stop(self.calls["paused"], "paused")
        self.store.add(self.calls["other"], "channel-3", Path(""), "ハラン", "channel", "bellteam", "claude-code", "/work")
        self.store.add(self.calls["seat"], "cb-claude-work-12345678", Path(""), "ナユタ", "hosted", "bellteam", "claude-code", "/work")
        self.store.add(self.calls["closed"], "channel-1", Path(""), "ベル", "channel", "bellteam", "claude-code", "/work")
        self.store.stop(self.calls["closed"], "closed")
        self.owners = AsyncMock(return_value={"channel-1": "conversation-1", "channel-2": "conversation-1", "channel-3": "conversation-2"})
        self.handoff = AsyncMock(return_value={"handoff_id": "h", "source_session_id": "conversation-1", "state": "launching"})
        for target, name, value in ((handoff_hint.claude_channel, "owners", self.owners),
                                    (handoff_hint.conversation, "claude_handoff", self.handoff)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_calls_the_predecessor_held_are_listed_and_nothing_is_changed(self):
        found = await handoff_hint.inherited("h")
        self.assertEqual([call["session_id"] for call in found], [self.calls["mine"], self.calls["paused"]])
        self.handoff.assert_awaited_once_with("h")
        self.owners.assert_awaited_once_with(["channel-1", "channel-2", "channel-3"])  # 生きている通話の受け口だけを、1回で引く
        text = handoff_hint.hint(found)
        self.assertIn(f"session_id={self.calls['mine']} 相手=リリーバイス", text)
        self.assertIn("call_adopt", text)
        self.assertNotIn(self.calls["other"], text)
        self.assertEqual(self.store.subscription(self.calls["mine"])["thread_id"], "channel-1")  # 届け先は替えない

    async def test_nothing_is_said_when_there_is_nothing_to_take_over(self):
        self.assertEqual(handoff_hint.hint([]), "")
        self.handoff.return_value = None  # Throughline が無い・記録が無い
        self.assertEqual(await handoff_hint.inherited("h"), [])
        self.handoff.return_value = {"handoff_id": "h", "source_session_id": "conversation-9", "state": "sent"}
        self.assertEqual(await handoff_hint.inherited("h"), [])  # 前の会話は通話を持っていない
        self.handoff.return_value = {"handoff_id": "h", "source_session_id": "conversation-1", "state": "sent"}
        self.owners.side_effect = DeliveryError("CLAUDE_CHANNEL_UNAVAILABLE", "入口がありません")
        self.assertEqual(await handoff_hint.inherited("h"), [])

    def test_stopped_conversation_counts_as_handed_off(self):
        for state in ("stopped", "launching", "launched", "sent", "unknown"):
            self.assertEqual(conversation.claude_retired({"state": state, "successor_short_id": "e50377e1"}), "e50377e1")
        for state in ("requested", "failed"):
            self.assertIsNone(conversation.claude_retired({"state": state}))
        self.assertEqual(conversation.claude_retired({"state": "stopped"}), "?")  # 後継がまだ立っていない
