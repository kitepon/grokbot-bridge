"""Caller identity: tokens bound to systems, checked over real HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from call_bridge import server
from call_bridge import auth as auth_module
from call_bridge.auth import LEGACY, OPEN, UNAUTHENTICATED, AuthError, Authenticator, check_open, is_party
from call_bridge.db import CallStore

BELLTEAM = "bellteam-token"
GROKBOT = "grokbot-token"
LOCAL = "local-token"
OPS = "ops-token"
SHARED = "shared-token"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _write_tokens(directory: str) -> str:
    path = Path(directory) / "tokens.json"
    path.write_text(json.dumps({"tokens": [
        {"name": "bellteam", "sha256": _sha(BELLTEAM), "system": "bellteam", "caller_id_header": True},
        {"name": "grokbot", "sha256": _sha(GROKBOT), "system": "grokbot"},
        {"name": "macbook", "sha256": _sha(LOCAL), "system": "local"},
        {"name": "ops", "sha256": _sha(OPS), "ops": True},
    ]}), encoding="utf-8")
    return str(path)


def _request(token: str, caller_id: str | None = None) -> httpx.Headers:
    """Case-insensitive headers, as Starlette hands them to the authenticator."""
    return httpx.Headers(_headers(token, caller_id))


def _headers(token: str, caller_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if caller_id:
        headers["X-Call-Bridge-Caller-Id"] = caller_id
    return headers


class AuthenticatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth = Authenticator(SHARED, _write_tokens(self.tmp.name))

    def test_token_selects_system_and_header_names_bellteam_bot(self) -> None:
        p = self.auth.authenticate(_request(BELLTEAM, "bot-a"))
        self.assertEqual((p.system, p.id), ("bellteam", "bot-a"))
        p = self.auth.authenticate(_request(BELLTEAM))
        self.assertEqual((p.system, p.id), ("bellteam", None))
        self.assertEqual(self.auth.authenticate(_request(GROKBOT)).system, "grokbot")

    def test_wrong_token_and_header_without_permission_are_refused(self) -> None:
        self.assertIsNone(self.auth.authenticate(_request("nope")))
        self.assertIsNone(self.auth.authenticate({}))
        with self.assertRaises(AuthError):
            self.auth.authenticate(_request(GROKBOT, "someone"))

    def test_legacy_token_is_unrestricted_and_ignores_the_header(self) -> None:
        with self.assertLogs("call_bridge.auth", "WARNING"):
            self.assertIs(self.auth.authenticate(_request(SHARED)), LEGACY)
        self.assertIs(self.auth.authenticate(_request(SHARED, "bot-a")), LEGACY)

    def test_legacy_warning_is_limited_to_once_per_five_minutes(self) -> None:
        clock = [1000.0]
        with mock.patch.object(auth_module.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(auth_module.log, "warning") as warning:
            self.auth.authenticate(_request(SHARED))
            clock[0] += 299
            self.auth.authenticate(_request(SHARED))
            self.assertEqual(warning.call_count, 1)
            clock[0] += 1
            self.auth.authenticate(_request(SHARED))
            self.assertEqual(warning.call_count, 2)

    def test_legacy_only_does_not_warn(self) -> None:
        with mock.patch.object(auth_module.log, "warning") as warning:
            self.assertIs(Authenticator(SHARED).authenticate(_request(SHARED)), LEGACY)
        warning.assert_not_called()

    def test_no_configuration_is_open_with_ops(self) -> None:
        principal = Authenticator().authenticate({})
        self.assertIs(principal, OPEN)
        self.assertTrue(principal.unrestricted and principal.ops)

    def _bad_file(self, content: str) -> str:
        path = Path(self.tmp.name) / "bad.json"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_invalid_token_files_stop_with_one_line_errors(self) -> None:
        entry = {"name": "x", "sha256": _sha("x"), "system": "grokbot"}
        cases = {
            "empty list": json.dumps({"tokens": []}),
            "empty object": "{}",
            "not an object": "[]",
            "broken json": "{",
            "ops as string": json.dumps({"tokens": [{**entry, "ops": "false"}]}),
            "header flag as string": json.dumps({"tokens": [{**entry, "caller_id_header": "true"}]}),
            "system not a string": json.dumps({"tokens": [{**entry, "system": 1}]}),
            "unknown system": json.dumps({"tokens": [{**entry, "system": "other"}]}),
            "bad sha": json.dumps({"tokens": [{**entry, "sha256": "zz"}]}),
            "duplicate sha": json.dumps({"tokens": [entry, {**entry, "name": "y"}]}),
            "no system without ops": json.dumps({"tokens": [{"name": "x", "sha256": _sha("x")}]}),
        }
        for label, content in cases.items():
            with self.subTest(label), self.assertRaises(AuthError) as caught:
                Authenticator("", self._bad_file(content))
            self.assertNotIn("\n", str(caught.exception))
        with self.assertRaises(AuthError):
            Authenticator("", str(Path(self.tmp.name) / "missing.json"))

    def test_named_file_turns_auth_on_even_without_legacy_token(self) -> None:
        auth = Authenticator("", _write_tokens(self.tmp.name))
        self.assertTrue(auth.enabled)
        self.assertIsNone(auth.authenticate({}))

    def test_in_process_call_is_open_only_when_auth_is_off(self) -> None:
        with mock.patch.object(server, "AUTH", self.auth):
            self.assertIs(server._principal(None), UNAUTHENTICATED)
        with mock.patch.object(server, "AUTH", Authenticator()):
            self.assertIs(server._principal(None), OPEN)

    def test_request_without_middleware_matches_nothing_when_auth_is_on(self) -> None:
        request = mock.Mock()
        request.state = mock.Mock(spec=[])
        with mock.patch.object(server, "AUTH", self.auth):
            principal = server._request_principal(request)
        self.assertFalse(principal.unrestricted)
        self.assertFalse(is_party(principal, {"local_system": "local", "local_id": "x"}, "local"))
        self.assertIsNotNone(check_open(principal, "local", "x"))

    def test_party_rules(self) -> None:
        sess = {"local_system": "bellteam", "local_id": "bot-a",
                "member_system": "grokbot", "member_id": "g-1"}
        bot_a = self.auth.authenticate(_request(BELLTEAM, "bot-a"))
        bot_b = self.auth.authenticate(_request(BELLTEAM, "bot-b"))
        grok = self.auth.authenticate(_request(GROKBOT))
        self.assertTrue(is_party(bot_a, sess, "local"))
        self.assertFalse(is_party(bot_b, sess, "local"))
        self.assertFalse(is_party(bot_a, sess, "member"))
        self.assertTrue(is_party(grok, sess, "member"))
        self.assertFalse(is_party(grok, sess, "local"))
        self.assertIsNone(check_open(bot_a, "bellteam", "bot-a"))
        self.assertIsNotNone(check_open(bot_a, "bellteam", "bot-b"))
        self.assertIsNotNone(check_open(grok, "local", "x"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpIdentityTest(unittest.TestCase):
    """Runs the real Streamable HTTP app so tools see the authenticated request."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patches = [
            mock.patch.object(server, "AUTH", Authenticator(SHARED, _write_tokens(cls.tmp.name))),
            mock.patch.object(server, "store", CallStore(Path(cls.tmp.name) / "calls.db")),
            mock.patch("call_bridge.deliver.notify_ring", return_value={"status": "ok", "detail": "test"}),
            mock.patch.object(server, "resolve_member_agent_id",
                              return_value={"ok": True, "id": "g-1", "name": "マリアン"}),
            mock.patch("call_bridge.deliver.send_delivery"),
        ]
        for patch in cls.patches:
            patch.start()
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        app = server.mcp.streamable_http_app()
        app.add_middleware(server.BearerAuthMiddleware)
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        for patch in reversed(cls.patches):
            patch.stop()
        cls.tmp.cleanup()

    def _tool(self, headers: dict[str, str], name: str, **arguments) -> dict:
        async def run() -> dict:
            async with streamablehttp_client(f"{self.base}/mcp", headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    return json.loads(result.content[0].text)
        return asyncio.run(run())

    def _open_as_bot_a(self) -> str:
        opened = self._tool(_headers(BELLTEAM, "bot-a"), "call_open", local_id="bot-a",
                            local_label="A", member_name="マリアン", local_system="bellteam")
        self.assertEqual(opened["local_id"], "bot-a", opened)
        return opened["session_id"]

    def test_a_session_id_from_before_a_restart_still_works(self) -> None:
        """Cursor keeps its old Mcp-Session-Id after the bridge restarts and never
        re-initializes. The call must go through, as the caller of this request."""
        sid = self._open_as_bot_a()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "call_info", "arguments": {"session_id": sid}}}

        def call(caller_id: str) -> dict:
            response = httpx.post(f"{self.base}/mcp", json=body, timeout=10, headers={
                **_headers(BELLTEAM, caller_id),
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": "0123456789abcdef0123456789abcdef",
            })
            self.assertEqual(response.status_code, 200, response.text)
            data = next(line for line in response.text.splitlines() if line.startswith("data: "))
            return json.loads(json.loads(data[6:])["result"]["content"][0]["text"])

        self.assertEqual(call("bot-a")["session_id"], sid)
        self.assertEqual(call("bot-b")["error"], "forbidden")

    def test_open_rejects_a_caller_claiming_another_identity(self) -> None:
        other_bot = self._tool(_headers(BELLTEAM, "bot-a"), "call_open", local_id="bot-b",
                               local_label="B", member_name="マリアン", local_system="bellteam")
        self.assertEqual(other_bot["error"], "forbidden")
        other_system = self._tool(_headers(LOCAL), "call_open", local_id="bot-a",
                                  local_label="A", member_name="マリアン", local_system="bellteam")
        self.assertEqual(other_system["error"], "forbidden")

    def test_only_parties_can_reply_poll_and_see_the_call(self) -> None:
        sid = self._open_as_bot_a()
        forged = self._tool(_headers(BELLTEAM, "bot-b"), "call_send",
                            session_id=sid, from_party="local", message="x")
        self.assertEqual(forged["error"], "forbidden")
        forged_member = self._tool(_headers(LOCAL), "call_send",
                                   session_id=sid, from_party="member", message="x")
        self.assertEqual(forged_member["error"], "forbidden")
        reply = self._tool(_headers(GROKBOT), "call_send",
                           session_id=sid, from_party="member", message="返事")
        self.assertEqual(reply["seq"], 1, reply)
        polled = self._tool(_headers(BELLTEAM, "bot-a"), "call_poll", session_id=sid, party="local")
        self.assertEqual([m["message"] for m in polled["messages"]], ["返事"])
        self.assertEqual(self._tool(_headers(BELLTEAM, "bot-b"), "call_poll",
                                    session_id=sid, party="local")["error"], "forbidden")
        self.assertEqual(self._tool(_headers(LOCAL), "call_info", session_id=sid)["error"], "forbidden")
        listed = self._tool(_headers(BELLTEAM, "bot-b"), "call_list")
        self.assertNotIn(sid, [s["session_id"] for s in listed["sessions"]])
        listed = self._tool(_headers(GROKBOT), "call_list")
        self.assertIn(sid, [s["session_id"] for s in listed["sessions"]])

    def test_hangup_needs_a_party_or_ops(self) -> None:
        sid = self._open_as_bot_a()
        self.assertEqual(self._tool(_headers(GROKBOT), "call_hangup", session_id=sid,
                                    by_party="ops")["error"], "forbidden")
        self.assertEqual(self._tool(_headers(LOCAL), "call_hangup", session_id=sid,
                                    by_party="member")["error"], "forbidden")
        done = self._tool(_headers(OPS), "call_hangup", session_id=sid, by_party="ops")
        self.assertEqual(done["status"], "hungup")

    def test_legacy_token_keeps_current_behavior(self) -> None:
        sid = self._open_as_bot_a()
        reply = self._tool(_headers(SHARED), "call_send", session_id=sid, from_party="member", message="旧")
        self.assertEqual(reply["seq"], 1, reply)

    def test_grokbot_token_without_local_system_calls_as_grokbot(self) -> None:
        opened = self._tool(_headers(GROKBOT), "call_open", local_id="g-2", local_label="G",
                            member_name="マリアン")
        self.assertEqual(opened["local_system"], "grokbot", opened)
        sent = self._tool(_headers(GROKBOT), "call_send", session_id=opened["session_id"],
                          from_party="local", message="x")
        self.assertNotEqual(sent.get("error"), "forbidden", sent)

    def test_forbidden_header_body_does_not_name_the_token(self) -> None:
        with httpx.Client(base_url=self.base, timeout=10) as client:
            response = client.get("/v0/directory", headers=_headers(GROKBOT, "g-1"))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("grokbot", response.text)

    def _current_behavior(self, headers: dict[str, str]) -> None:
        opened = self._tool(headers, "call_open", local_id="dev", local_label="D", member_name="マリアン")
        self.assertEqual(opened["local_system"], "local", opened)
        sid = opened["session_id"]
        self.assertEqual(self._tool(headers, "call_send", session_id=sid, from_party="member",
                                    message="返事")["seq"], 1)
        polled = self._tool(headers, "call_poll", session_id=sid, party="local")
        self.assertEqual([m["message"] for m in polled["messages"]], ["返事"])
        self.assertEqual(self._tool(headers, "call_info", session_id=sid)["message_count"], 1)
        self.assertIn(sid, [s["session_id"] for s in self._tool(headers, "call_list")["sessions"]])
        self.assertEqual(self._tool(headers, "call_hangup", session_id=sid, by_party="ops")["status"], "hungup")
        with httpx.Client(base_url=self.base, timeout=10) as client:
            rest = client.post("/v0/sessions", headers=headers, json={
                "local_id": "dev", "local_label": "D", "member_name": "マリアン"})
            self.assertEqual(rest.status_code, 201, rest.text)
            self.assertEqual(rest.json()["local_system"], "local")
            rsid = rest.json()["session_id"]
            self.assertEqual(client.post(f"/v0/sessions/{rsid}/messages", headers=headers,
                                         json={"from_party": "member", "message": "ok"}).status_code, 200)
            self.assertEqual(client.get(f"/v0/sessions/{rsid}/poll", params={"party": "local"},
                                        headers=headers).status_code, 200)

    def test_without_token_file_legacy_token_keeps_every_path(self) -> None:
        with mock.patch.object(server, "AUTH", Authenticator(SHARED)):
            self._current_behavior(_headers(SHARED))

    def test_dev_mode_keeps_every_path_including_ops_hangup(self) -> None:
        with mock.patch.object(server, "AUTH", Authenticator()):
            self._current_behavior({})

    def test_rest_routes_use_the_same_identity(self) -> None:
        with httpx.Client(base_url=self.base, timeout=10) as client:
            self.assertEqual(client.get("/v0/directory").status_code, 401)
            forged = client.post("/v0/sessions", headers=_headers(GROKBOT), json={
                "local_id": "bot-a", "local_label": "A", "member_name": "マリアン", "local_system": "bellteam"})
            self.assertEqual(forged.status_code, 403)
            opened = client.post("/v0/sessions", headers=_headers(BELLTEAM, "bot-a"), json={
                "local_id": "bot-a", "local_label": "A", "member_name": "マリアン", "local_system": "bellteam"})
            self.assertEqual(opened.status_code, 201, opened.text)
            sid = opened.json()["session_id"]
            self.assertEqual(client.get(f"/v0/sessions/{sid}/poll", params={"party": "local"},
                                        headers=_headers(BELLTEAM, "bot-b")).status_code, 403)
            sent = client.post(f"/v0/sessions/{sid}/messages", headers=_headers(GROKBOT),
                               json={"from_party": "member", "message": "ok"})
            self.assertEqual(sent.status_code, 200, sent.text)
            bad_header = client.get("/v0/directory", headers=_headers(GROKBOT, "g-1"))
            self.assertEqual(bad_header.status_code, 403)

    def _call_with_one_reply(self) -> str:
        opened = self._tool(_headers(LOCAL), "call_open", local_id="mac-1", local_label="ベル", member_name="マリアン")
        self.assertEqual(opened["local_system"], "local", opened)
        sid = opened["session_id"]
        reply = self._tool(_headers(GROKBOT), "call_send", session_id=sid, from_party="member", message="返事")
        self.assertEqual((reply["seq"], reply["delivery"]["status"]), (1, "stored"), reply)
        return sid

    def test_local_reports_a_receipt_and_the_member_reads_it_in_call_info(self) -> None:
        sid = self._call_with_one_reply()
        done = self._tool(_headers(LOCAL), "call_receipt", session_id=sid, seq=1, state="relaunched",
                          detail="stopped conversation; new task in the same folder", conversation="hosted:bell")
        self.assertEqual(done["state"], "relaunched", done)
        info = self._tool(_headers(GROKBOT), "call_info", session_id=sid)
        self.assertEqual([(r["seq"], r["state"], r["conversation"]) for r in info["local_delivery"]],
                         [(1, "relaunched", "hosted:bell")])

    def test_only_the_local_party_may_report_a_receipt(self) -> None:
        sid = self._call_with_one_reply()
        self.assertEqual(self._tool(_headers(GROKBOT), "call_receipt", session_id=sid, seq=1,
                                    state="started")["error"], "forbidden")
        self.assertEqual(self._tool(_headers(BELLTEAM, "bot-b"), "call_receipt", session_id=sid, seq=1,
                                    state="started")["error"], "forbidden")
        self.assertEqual(self._tool(_headers(LOCAL), "call_receipt", session_id=sid, seq=9,
                                    state="started")["error"], "rejected")
        self.assertEqual(self._tool(_headers(LOCAL), "call_receipt", session_id=sid, seq=1,
                                    state="read")["error"], "rejected")

    def test_history_is_for_parties_and_is_not_a_fetch(self) -> None:
        sid = self._call_with_one_reply()
        page = self._tool(_headers(GROKBOT), "call_history", session_id=sid)
        self.assertEqual([(m["seq"], m["from_party"]) for m in page["messages"]], [(1, "member")])
        self.assertEqual(self._tool(_headers(BELLTEAM, "bot-b"), "call_history", session_id=sid)["error"], "forbidden")
        self.assertEqual(self._tool(_headers(LOCAL), "call_history", session_id="none")["error"], "not_found")
        self.assertIsNone(server.store.get_session(sid)["local_seen_at"])

    def test_rest_peek_leaves_the_delivered_mark_and_rest_receipt_stores(self) -> None:
        sid = self._call_with_one_reply()

        def delivered() -> int:
            with server.store._conn() as conn:
                return conn.execute("SELECT delivered_to_local FROM messages WHERE session_id = ? AND seq = 1",
                                    (sid,)).fetchone()[0]

        with httpx.Client(base_url=self.base, timeout=10) as client:
            peeked = client.get(f"/v0/sessions/{sid}/poll", params={"party": "local", "peek": "1"}, headers=_headers(LOCAL))
            self.assertEqual([m["seq"] for m in peeked.json()["messages"]], [1], peeked.text)
            self.assertEqual(delivered(), 0)
            self.assertIsNotNone(server.store.get_session(sid)["local_seen_at"])
            client.get(f"/v0/sessions/{sid}/poll", params={"party": "local"}, headers=_headers(LOCAL))
            self.assertEqual(delivered(), 1)
            stored = client.post(f"/v0/sessions/{sid}/receipts", headers=_headers(LOCAL),
                                 json={"seq": 1, "state": "submitted", "conversation": "codex:t-1"})
            self.assertEqual(stored.status_code, 200, stored.text)
            self.assertEqual(client.post(f"/v0/sessions/{sid}/receipts", headers=_headers(GROKBOT),
                                         json={"seq": 1, "state": "started"}).status_code, 403)
            self.assertEqual(client.post(f"/v0/sessions/{sid}/receipts", headers=_headers(LOCAL),
                                         json={"seq": "1", "state": "started"}).status_code, 400)
            history = client.get(f"/v0/sessions/{sid}/history", headers=_headers(LOCAL))
            self.assertEqual((history.status_code, history.json()["more"]), (200, False), history.text)
            self.assertEqual(client.get(f"/v0/sessions/{sid}/history", headers=_headers(BELLTEAM, "bot-b")).status_code, 403)
        self.assertEqual([r["state"] for r in server.store.receipts(sid)], ["submitted"])


if __name__ == "__main__":
    unittest.main()
