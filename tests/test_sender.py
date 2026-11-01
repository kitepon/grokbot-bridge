"""Local call_send must name a sender, and a bound token cannot pretend to be someone else."""

from __future__ import annotations

import os
import asyncio
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
from call_bridge import local  # noqa: E402
from call_bridge.sender import PREFACE, utf16_code_units, with_sender_preface  # noqa: E402


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

    def test_local_caller_id_uses_the_registered_token_name(self) -> None:
        from call_bridge.directory import member_display_name

        self.assertIsNone(member_display_name("local", "approval-box"))
        self.assertIsNone(member_display_name("local", "local-muse-quolu"))
        for mode in ("courier", "marian"):
            with self.subTest(mode=mode):
                os.environ["CALL_BRIDGE_GROKBOT_DELIVERY"] = mode
                cases = (
                    ("approval-box", "approval-box"),
                    ("local-muse-quolu", "muse-1"),
                )
                for registered, caller_id in cases:
                    session = self.store.open_session(
                        caller_id, registered, "ラピ", member_id="rapi-agent", local_system="local",
                    )
                    principal = Principal(name=registered, system="local", id=caller_id)
                    with mock.patch.object(server, "store", self.store), \
                         mock.patch("call_bridge.deliver.notify_ring", return_value={"status": "ok", "detail": "http 204"}) as ring:
                        sent = server._send(
                            principal, session["session_id"], "local", "本文", False, registered, "ローカル",
                        )
                        omitted = server._send(
                            principal, session["session_id"], "local", "続き", False, None, None,
                        )
                        spoofed = server._send(
                            principal, session["session_id"], "local", "偽装", False, "別人", "ローカル",
                        )
                        id_as_name = server._send(
                            principal, session["session_id"], "local", "偽装", False, caller_id, "local",
                        )
                    self.assertNotIn("error", sent, sent)
                    self.assertNotIn("not in the directory", sent.get("detail", ""))
                    preface = f"これはオーナー本人ではなく、ローカルチームの{registered}からのメッセージです。"
                    self.assertTrue(sent["message"].startswith(preface), sent["message"])
                    self.assertNotIn("error", omitted, omitted)
                    self.assertTrue(omitted["message"].startswith(preface), omitted["message"])
                    self.assertEqual(spoofed["error"], "forbidden")
                    if caller_id != registered:
                        self.assertEqual(id_as_name["error"], "forbidden")
                        self.assertNotIn(caller_id, sent["message"])
                    else:
                        self.assertNotIn("error", id_as_name, id_as_name)
                    if mode == "marian":
                        self.assertEqual(sent["delivery"]["status"], "delivered")
                        self.assertGreaterEqual(ring.call_count, 1)
                    else:
                        ring.assert_not_called()
                        self.assertEqual(sent["delivery"]["status"], "queued")

    def test_sender_name_limit_follows_utf16_code_units(self) -> None:
        """80, 81, and 100 UTF-16 units pass. 101 does not. Names are not shortened."""
        session = self.store.open_session("local-1", "Cursor", "ラピ", member_id="rapi-agent")
        before = self.store.session_info(session["session_id"])["message_count"]

        def send(name: str) -> dict:
            with mock.patch.object(server, "store", self.store):
                return server._send(OPEN, session["session_id"], "local", "本文", False, name, "ローカル")

        for units in (80, 81, 100):
            name = "A" * units
            self.assertEqual(len(name), units)
            self.assertEqual(utf16_code_units(name), units)
            sent = send(name)
            self.assertNotIn("error", sent, sent)
            self.assertIn(name + "からのメッセージです。\n本文", sent["message"])

        seat = self.agents / "long-bot"
        seat.mkdir()
        (seat / "profile.json").write_text('{"name":"%s"}' % ("B" * 81), encoding="utf-8")
        booked = self.store.open_session(
            "long-bot", "席", "ラピ", member_id="rapi-agent", local_system="grokbot",
        )
        principal = Principal(name="grokbot", system="grokbot", id="long-bot")
        with mock.patch.object(server, "store", self.store):
            from_book = server._send(principal, booked["session_id"], "local", "本文", False, None, None)
            spoofed = server._send(principal, booked["session_id"], "local", "本文", False, "C" * 81, None)
        self.assertNotIn("error", from_book, from_book)
        self.assertIn("GrokBotチームの" + ("B" * 81) + "からのメッセージです。", from_book["message"])
        self.assertEqual(spoofed["error"], "forbidden")

        too_long = "A" * 101
        rejected = send(too_long)
        self.assertEqual(rejected["error"], "rejected")
        self.assertNotIn(too_long, rejected.get("detail", ""))
        stored = self.store.history(session["session_id"])["messages"]
        self.assertEqual(len(stored), before + 3)
        self.assertTrue(all("A" * 101 not in item["message"] for item in stored))

        kana = "あ" * 100
        self.assertEqual(utf16_code_units(kana), 100)
        sent = send(kana)
        self.assertNotIn("error", sent, sent)
        self.assertIn(kana, sent["message"])
        self.assertEqual(send("あ" * 101)["error"], "rejected")

        # U+1F600 is one Python code point and one UTF-16 surrogate pair.
        pair = "\U0001F600"
        self.assertEqual(len(pair), 1)
        self.assertEqual(utf16_code_units(pair), 2)
        fifty = pair * 50
        self.assertEqual(len(fifty), 50)
        self.assertEqual(utf16_code_units(fifty), 100)
        sent = send(fifty)
        self.assertNotIn("error", sent, sent)
        self.assertIn(fifty, sent["message"])
        one_over = fifty + "x"
        self.assertEqual(len(one_over), 51)
        self.assertEqual(utf16_code_units(one_over), 101)
        self.assertEqual(send(one_over)["error"], "rejected")
        hundred_pairs = pair * 100
        self.assertEqual(len(hundred_pairs), 100)
        self.assertEqual(utf16_code_units(hundred_pairs), 200)
        self.assertEqual(send(hundred_pairs)["error"], "rejected")
        exact = pair * 49 + "あ" * 2
        self.assertEqual(utf16_code_units(exact), 100)
        self.assertNotEqual(len(exact), 100)
        sent = send(exact)
        self.assertNotIn("error", sent, sent)
        self.assertIn(exact, sent["message"])

        for broken in ("行\n名", "行\r名", "行\x00名"):
            self.assertEqual(send(broken)["error"], "rejected", broken)

    def test_member_reply_has_no_preface_and_does_not_need_a_sender(self) -> None:
        session = self.store.open_session("local-1", "Cursor", "ラピ", member_id="rapi-agent")
        result = dispatch_send(self.store, session["session_id"], "member", "確認しました")
        self.assertEqual(result["message"], "確認しました")
        self.assertNotIn("オーナー本人", result["message"])

    def test_legacy_local_input_uses_authenticated_connection_name(self) -> None:
        session = self.store.open_session("old-client", "Unverified label", "ラピ", member_id="rapi-agent")
        principal = Principal(name="local-macbook", system="local")
        with mock.patch.object(server, "store", self.store):
            sent = server._send(principal, session["session_id"], "local", "本文", False)
            blank = server._send(principal, session["session_id"], "local", "空欄", False, "", None)
            spoofed = server._send(principal, session["session_id"], "local", "別所属", False, None, "BellTeam")
            unbound = server._send(OPEN, session["session_id"], "local", "匿名", False)
        self.assertNotIn("error", sent, sent)
        self.assertTrue(sent["message"].startswith(PREFACE.format(team="ローカル", name="local-macbook")))
        self.assertNotIn("Unverified label", sent["message"])
        self.assertEqual(blank["error"], "rejected")
        self.assertEqual(spoofed["error"], "forbidden")
        self.assertEqual(unbound["error"], "rejected")
        self.assertEqual(self.store.session_info(session["session_id"])["message_count"], 1)

    def test_legacy_grokbot_input_resolves_session_caller_in_directory(self) -> None:
        session = self.store.open_session("rapi-agent", "Unverified label", "ラピ", member_id="other", local_system="grokbot")
        missing = self.store.open_session("not-registered", "ラピ", "ラピ", member_id="other", local_system="grokbot")
        principal = Principal(name="grokbot", system="grokbot")
        with mock.patch.object(server, "store", self.store):
            sent = server._send(principal, session["session_id"], "local", "本文", False)
            rejected = server._send(principal, missing["session_id"], "local", "偽装", False)
        self.assertNotIn("error", sent, sent)
        self.assertTrue(sent["message"].startswith(PREFACE.format(team="GrokBot", name="ラピ")))
        self.assertEqual(rejected["error"], "rejected")
        self.assertEqual(self.store.session_info(missing["session_id"])["message_count"], 0)

    def test_local_wrapper_preserves_explicit_blank_sender(self) -> None:
        with mock.patch.object(local, "_follow_speaker", new_callable=mock.AsyncMock, return_value=None), \
             mock.patch.object(local, "_remote_tool", new_callable=mock.AsyncMock, return_value={}) as remote:
            asyncio.run(local.call_send("test", "local", "本文", False, "", ""))
        self.assertEqual(remote.call_args.args[1]["sender_name"], "")
        self.assertEqual(remote.call_args.args[1]["sender_team"], "")

    def test_explicit_blank_fields_are_rejected_for_bound_callers(self) -> None:
        from call_bridge.sender import resolve_sender

        for principal in (Principal(name="local-macbook", system="local"),
                          Principal(name="approval-box", system="local", id="approval-box")):
            for name, team in (("", None), ("  ", None), ("approval-box", ""), ("approval-box", "  ")):
                with self.subTest(principal=principal, name=name, team=team):
                    self.assertEqual(resolve_sender(principal, name, team)["error"], "rejected")


if __name__ == "__main__":
    unittest.main()
