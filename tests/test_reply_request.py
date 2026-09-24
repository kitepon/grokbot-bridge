"""ローカル送信の返信依頼と通知の明示的な例外を確認する。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from call_bridge.db import CallStore


class ReplyRequestTest(unittest.TestCase):
    def test_local_message_requests_reply_unless_marked_as_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = CallStore(Path(temp) / "calls.sqlite")
            session_id = store.open_session("local-1", "ベル", "エード")["session_id"]

            request = store.send_message(session_id, "local", "要件を確認してください")
            notice = store.send_message(session_id, "local", "共有だけです", reply_required=False)
            member = store.send_message(session_id, "member", "確認しました")

            self.assertTrue(request["message"].startswith("要件を確認してください\n\n"))
            self.assertIn(f"session_id={session_id}", request["message"])
            self.assertIn("member として call_send で返事", request["message"])
            self.assertEqual(notice["message"], "共有だけです")
            self.assertEqual(member["message"], "確認しました")
            received = store.poll_messages(session_id, "member", mark_delivered=False)
            self.assertEqual([item["message"] for item in received["messages"]],
                             [request["message"], notice["message"]])


if __name__ == "__main__":
    unittest.main()
