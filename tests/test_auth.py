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
from call_bridge.auth import LEGACY, AuthError, Authenticator, check_open, is_party
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

    def test_legacy_token_is_unrestricted(self) -> None:
        with self.assertLogs("call_bridge.auth", "WARNING"):
            self.assertIs(self.auth.authenticate(_request(SHARED)), LEGACY)

    def test_no_configuration_is_open(self) -> None:
        self.assertTrue(Authenticator().authenticate({}).unrestricted)

    def test_entry_without_system_must_be_ops(self) -> None:
        path = Path(self.tmp.name) / "bad.json"
        path.write_text(json.dumps({"tokens": [{"name": "x", "sha256": _sha("x")}]}), encoding="utf-8")
        with self.assertRaises(AuthError):
            Authenticator("", str(path))

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
            mock.patch.object(server, "notify_wake", return_value={"status": "ok", "detail": "test"}),
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


if __name__ == "__main__":
    unittest.main()
