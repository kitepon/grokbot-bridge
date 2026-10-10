"""What the local side did with a member message, and the call history.

The cases over real HTTP are in test_auth.py: one FastMCP app starts once per process.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


from call_bridge.db import RECEIPT_STATES, CallStore
from call_bridge.deliver import dispatch_send



class ReceiptStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CallStore(Path(self.tmp.name) / "calls.db")
        self.sid = self.store.open_session("mac-1", "ベル", "トロニー", None, "bellteam", "bot-a", "local")["session_id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_receipt_is_kept_per_member_message_and_the_last_report_wins(self) -> None:
        self.store.send_message(self.sid, "local", "頼み")
        self.store.send_message(self.sid, "member", "返事")
        self.store.record_receipt(self.sid, 2, "submitted", None, "codex:t-1")
        last = self.store.record_receipt(self.sid, 2, "started", "woken", "codex:t-1")
        self.assertEqual((last["state"], last["detail"], last["conversation"]), ("started", "woken", "codex:t-1"))
        info = self.store.session_info(self.sid)
        self.assertEqual([(r["seq"], r["state"], r["detail"]) for r in info["local_delivery"]], [(2, "started", "woken")])

    def test_receipt_refuses_a_local_message_an_unknown_seq_and_an_unknown_state(self) -> None:
        self.store.send_message(self.sid, "local", "頼み")
        with self.assertRaises(ValueError):
            self.store.record_receipt(self.sid, 1, "submitted")
        with self.assertRaises(ValueError):
            self.store.record_receipt(self.sid, 9, "submitted")
        self.store.send_message(self.sid, "member", "返事")
        with self.assertRaises(ValueError):
            self.store.record_receipt(self.sid, 2, "read")
        with self.assertRaises(KeyError):
            self.store.record_receipt("no-such-session", 2, "submitted")
        self.assertEqual(self.store.receipts(self.sid), [])
        for state in RECEIPT_STATES:
            self.store.record_receipt(self.sid, 2, state)

    def test_history_returns_both_parties_in_order_without_marking_delivered(self) -> None:
        self.store.send_message(self.sid, "local", "一", reply_required=False)
        self.store.send_message(self.sid, "member", "二")
        self.store.send_message(self.sid, "local", "三", reply_required=False)
        page = self.store.history(self.sid, limit=2)
        self.assertEqual([(m["seq"], m["from_party"], m["message"]) for m in page["messages"]],
                         [(1, "local", "一"), (2, "member", "二")])
        self.assertTrue(page["more"])
        rest = self.store.history(self.sid, after_seq=page["latest_seq"])
        self.assertEqual([m["message"] for m in rest["messages"]], ["三"])
        self.assertFalse(rest["more"])
        # History is not a fetch: the reply is still waiting for the local side.
        polled = self.store.poll_messages(self.sid, "local", mark_delivered=False)
        self.assertEqual([m["seq"] for m in polled["messages"]], [2])
        with self.assertRaises(ValueError):
            self.store.history(self.sid, limit=0)
        with self.assertRaises(KeyError):
            self.store.history("no-such-session")

    def test_local_poll_records_when_the_local_side_last_asked(self) -> None:
        self.assertIsNone(self.store.get_session(self.sid)["local_seen_at"])
        self.store.poll_messages(self.sid, "member")
        self.assertIsNone(self.store.get_session(self.sid)["local_seen_at"])
        self.store.poll_messages(self.sid, "local", mark_delivered=False)
        self.assertIsNotNone(self.store.get_session(self.sid)["local_seen_at"])

    def test_member_reply_to_a_polling_caller_says_stored_and_when_it_last_fetched(self) -> None:
        first = dispatch_send(self.store, self.sid, "member", "返事")
        self.assertEqual(first["delivery"]["status"], "stored")
        self.assertIsNone(first["delivery"]["local_seen_at"])
        self.assertIn("has not fetched", first["delivery"]["detail"])
        self.store.poll_messages(self.sid, "local")
        seen = self.store.get_session(self.sid)["local_seen_at"]
        second = dispatch_send(self.store, self.sid, "member", "続き")
        self.assertEqual(second["delivery"], {
            "status": "stored", "detail": f"waiting for the caller to fetch; last fetch at {seen}", "local_seen_at": seen})

    def test_an_older_database_gains_the_new_column_and_table(self) -> None:
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE sessions (session_id TEXT PRIMARY KEY, local_id TEXT NOT NULL, local_label TEXT NOT NULL,
                    member_name TEXT NOT NULL, member_system TEXT NOT NULL DEFAULT 'grokbot', member_id TEXT,
                    local_system TEXT NOT NULL DEFAULT 'local', purpose TEXT, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, hangup_by TEXT, hangup_reason TEXT);
                INSERT INTO sessions VALUES ('s-1','mac-1','ベル','トロニー','bellteam','bot-a','local',NULL,'open','t','t',NULL,NULL);
            """)
        store = CallStore(path)
        self.assertIsNone(store.get_session("s-1")["local_seen_at"])
        self.assertEqual(store.session_info("s-1")["local_delivery"], [])


if __name__ == "__main__":
    unittest.main()
