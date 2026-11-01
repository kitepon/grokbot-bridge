"""Local call_send must name a sender, and a bound token cannot pretend to be someone else."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from call_bridge.auth import OPEN, Principal  # noqa: E402
from call_bridge.db import CallStore  # noqa: E402
from call_bridge.deliver import dispatch_send  # noqa: E402
from call_bridge import server  # noqa: E402
from call_bridge.sender import PREFACE, with_sender_preface  # noqa: E402


class SenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CallStore(Path(self.tmp.name) / "calls.db")
        self.agents = Path(self.tmp.name) / "agents"
        seat = self.agents / "rapi-agent"
        seat.mkdir(parents=True)
        (seat / "profile.json").write_text(
            '{"name":"ラピ","title":"インフラ統括","description":"役"}',
            encoding="utf-8",
        )
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "CALL_BRIDGE_AGENTS_ROOT",
                "CALL_BRIDGE_DIRECTORY",
                "CALL_BRIDGE_DIRECTORY_UNIX",
                "CALL_BRIDGE_DIRECTORY_URL",
                "CALL_BRIDGE_DIRECTORY_PUSH",
                "CALL_BRIDGE_GROKBOT_DELIVERY",
                "CALL_BRIDGE_BELLTEAM_UNIX",
            )
        }
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = str(self.agents)
        os.environ["CALL_BRIDGE_DIRECTORY"] = str(Path(self.tmp.name) / "no-snapshot.json")
        os.environ["CALL_BRIDGE_DIRECTORY_PUSH"] = str(Path(self.tmp.name) / "no-push.json")
        os.environ["CALL_BRIDGE_GROKBOT_DELIVERY"] = "courier"
        os.environ.pop("CALL_BRIDGE_DIRECTORY_UNIX", None)
        os.environ.pop("CALL_BRIDGE_DIRECTORY_URL", None)
        os.environ.pop("CALL_BRIDGE_BELLTEAM_UNIX", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_preface_is_the_fixed_sentence(self) -> None:
        text = with_sender_preface("状況を教えてください", "GrokBot", "ラピ")
        self.assertEqual(
            text,
            "これはオーナー本人ではなく、GrokBotチームのラピからのメッセージです。\n状況を教えてください",
        )
        self.assertEqual(
            PREFACE.format(team="BellTeam", name="リタ"),
            "これはオーナー本人ではなく、BellTeamチームのリタからのメッセージです。",
        )

    def test_blank_sender_is_rejected_and_stores_nothing(self) -> None:
        session = self.store.open_session("local-1", "Cursor", "ラピ", member_id="rapi-agent")
        for name, team in (("", "ローカル"), ("Cursor", ""), ("  ", "ローカル"), (None, None)):
            result = dispatch_send(self.store, session["session_id"], "local", "本文", sender_name=name, sender_team=team)
            self.assertEqual(result["error"], "rejected", result)
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 0)

    def test_unrestricted_connection_must_supply_both_and_the_preface_is_stored_once(self) -> None:
        session = self.store.open_session("local-1", "Cursor", "ラピ", member_id="rapi-agent")
        with mock.patch.object(server, "store", self.store):
            missing = server._send(OPEN, session["session_id"], "local", "本文", True, "Cursor", None)
            sent = server._send(OPEN, session["session_id"], "local", "本文", True, "Cursor", "local")
        self.assertEqual(missing["error"], "rejected")
        self.assertNotIn("error", sent)
        self.assertEqual(sent["message"].count("これはオーナー本人ではなく、"), 1)
        self.assertTrue(sent["message"].startswith(
            "これはオーナー本人ではなく、ローカルチームのCursorからのメッセージです。"
        ))
        self.assertIn("本文", sent["message"])

    def test_bound_team_is_filled_and_a_different_team_is_refused(self) -> None:
        session = self.store.open_session("g-1", "ラピ", "ラピ", member_id="rapi-agent", local_system="grokbot")
        principal = Principal(name="grokbot", system="grokbot")
        with mock.patch.object(server, "store", self.store):
            omitted = server._send(principal, session["session_id"], "local", "本文", False, "ラピ", None)
            spoofed = server._send(principal, session["session_id"], "local", "本文", False, "ラピ", "BellTeam")
            blank = server._send(principal, session["session_id"], "local", "本文", False, "  ", None)
        self.assertNotIn("error", omitted, omitted)
        self.assertTrue(omitted["message"].startswith(
            "これはオーナー本人ではなく、GrokBotチームのラピからのメッセージです。"
        ))
        self.assertEqual(spoofed["error"], "forbidden")
        self.assertEqual(blank["error"], "rejected")
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 1)

    def test_bound_caller_id_uses_the_directory_name(self) -> None:
        session = self.store.open_session("rapi-agent", "席", "ラピ", member_id="other", local_system="grokbot")
        principal = Principal(name="grokbot", system="grokbot", id="rapi-agent")
        with mock.patch.object(server, "store", self.store):
            filled = server._send(principal, session["session_id"], "local", "本文", False, None, None)
            spoofed = server._send(principal, session["session_id"], "local", "本文", False, "別人", "GrokBot")
        self.assertNotIn("error", filled, filled)
        self.assertTrue(filled["message"].startswith(
            "これはオーナー本人ではなく、GrokBotチームのラピからのメッセージです。"
        ))
        self.assertEqual(spoofed["error"], "forbidden")

    def test_member_reply_has_no_preface_and_does_not_need_a_sender(self) -> None:
        session = self.store.open_session("local-1", "Cursor", "ラピ", member_id="rapi-agent")
        result = dispatch_send(self.store, session["session_id"], "member", "確認しました")
        self.assertEqual(result["message"], "確認しました")
        self.assertNotIn("オーナー本人", result["message"])


if __name__ == "__main__":
    unittest.main()
