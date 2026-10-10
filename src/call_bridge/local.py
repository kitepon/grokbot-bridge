"""通話の返信を裏で受信して親へ配送するローカル MCP。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlsplit, urlunsplit
from urllib.request import url2pathname

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import Context, FastMCP

from . import aiterm
from . import conversation as talk
from . import relaunch
from .codex_delivery import (CodexRPC, DeliveryError, codex_home, conversation_missing, hook_delivery_state,
                             state_root, submit_reply, verify_parent)

log = logging.getLogger("call_bridge.local")
_DELIVERY_NAMESPACE = uuid.UUID("ddf85db7-8d27-4c57-8ba5-9ad498cd64c9")
_POLL_SECONDS = 2.0
# Consecutive polls without a readable token before the watcher gives up
# (about 30 seconds, enough to delete and recreate auth.json by hand).
_TOKEN_READ_ATTEMPTS = 15
# 止まっているかを見直す間隔。会話を読むたびに Codex の App Server を1回起こすので、取りに行く間隔より長くする。
_RECHECK_SECONDS = 30.0
# キューへ入れてから、動いたかを確かめるまで。配送パッケージが寝た会話を起こす（15秒後に見直し、30秒待つ）のを待つ。
_CONFIRM_AFTER_SECONDS = 50.0
# 途中で止められた直後は、Throughline が続きの会話を作っている途中の事がある。これより新しい中断は待つ。
_INTERRUPT_SETTLE_SECONDS = 120.0
_MODES = ("queue", "exec", "hosted", "manual")
_HARNESSES = ("codex", "claude-code", "cursor", "grok")
_DONE = ("submitted", "injected", "relaunched", "fetched")


def _config() -> dict[str, str]:
    file = state_root() / "config.json"
    if not file.exists():
        return {}
    value = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DeliveryError("LOCAL_CONFIG_INVALID", "ローカル設定が不正です")
    return value


def _url() -> str:
    url = os.environ.get("CALL_BRIDGE_MCP_URL") or _config().get("mcp_url", "https://call.kitepon.dev/mcp")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.path.endswith("/mcp"):
        raise DeliveryError("BRIDGE_URL_INVALID", "CALL_BRIDGE_MCP_URL は /mcp で終わる HTTP URL にしてください")
    return url


def _rest_url(session_id: str) -> str:
    parts = urlsplit(_url())
    prefix = parts.path[:-4]
    return urlunsplit((parts.scheme, parts.netloc, f"{prefix}/v0/sessions/{session_id}/poll", "", ""))


def _headers() -> dict[str, str]:
    # The environment variable wins over auth.json and is fixed for the life of the
    # process; only auth.json can be rotated under a running MCP or watcher.
    config = _config()
    token_env = config.get("token_env", "CALL_BRIDGE_TOKEN")
    token = os.environ.get(token_env, "").strip()
    if not token and config.get("enabled") is True:
        file = state_root() / "auth.json"
        if file.exists():
            try:
                value = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise DeliveryError("BRIDGE_TOKEN_INVALID", "ローカル通話認証を読めません") from error
            if not isinstance(value, dict) or not isinstance(value.get("token"), str):
                raise DeliveryError("BRIDGE_TOKEN_INVALID", "ローカル通話認証の形式が不正です")
            token = value["token"].strip()
    if not token:
        raise DeliveryError("BRIDGE_TOKEN_MISSING", f"{token_env} がありません")
    return {"Authorization": f"Bearer {token}"}


def _claim_session(session_id: str) -> int | None:
    fd = os.open(state_root() / f"{session_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


async def _remote_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with streamablehttp_client(_url(), headers=_headers()) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    if result.isError:
        detail = " ".join(item.text for item in result.content if item.type == "text")
        raise DeliveryError("BRIDGE_TOOL_FAILED", detail or name)
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for item in result.content:
        if item.type == "text":
            value = json.loads(item.text)
            if isinstance(value, dict):
                return value
    raise DeliveryError("BRIDGE_RESPONSE_INVALID", f"{name} の応答を認識できません")


async def codex_conversation(thread_id: str, home: Path) -> talk.Conversation:
    async with CodexRPC(home) as rpc:
        return await talk.read_conversation(rpc.request, thread_id, conversation_missing)


async def codex_queued(thread_id: str, home: Path, delivery_id: str) -> tuple[bool, talk.Conversation]:
    """この配送の文がまだキューにあるかと、その時の会話の状態。"""
    async with CodexRPC(home) as rpc:
        queued = await talk.queued_entry(rpc.request, thread_id, delivery_id) is not None
        found = await talk.read_conversation(rpc.request, thread_id, conversation_missing)
    return queued, found


async def codex_withdraw(thread_id: str, home: Path, delivery_id: str) -> str:
    """キューの文を取り消す。withdrawn＝取り消した、gone＝もうキューに無い（番になった）、stuck＝残ったまま。"""
    async with CodexRPC(home) as rpc:
        if await talk.withdraw(rpc.request, thread_id, delivery_id):
            return "withdrawn"
        return "gone" if await talk.queued_entry(rpc.request, thread_id, delivery_id) is None else "stuck"


async def fetch_body(session_id: str, seq: int) -> str:
    """取り消した返信の本文を、通話の履歴から読み直す。"""
    url = _rest_url(session_id).removesuffix("/poll") + "/history"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers=_headers(), params={"after_seq": seq - 1, "limit": 1})
        response.raise_for_status()
        rows = response.json().get("messages")
    if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get("seq") != seq
            or rows[0].get("from_party") != "member" or not isinstance(rows[0].get("message"), str)):
        raise DeliveryError("BRIDGE_HISTORY_INVALID", "取り消した返信の本文を読み直せません", outcome_unknown=True)
    return rows[0]["message"]


_REPLY_LABELS = {"grokbot": "GrokBot", "bellteam": "BellTeam"}


def reply_text(subscription: dict[str, Any], seq: int, body: str) -> str:
    """返信元の所属を見出しに付ける。所属を記録する前の通話は所属を名乗らない。"""
    label = _REPLY_LABELS.get(subscription.get("member_system"))
    head = f"{label} の返信です。" if label else "通話の返信です。"
    return (f"{head}session_id={subscription['session_id']} seq={seq} "
            f"member={subscription['member_name']}\n\n{body}")


class LocalStore:
    def __init__(self, root: Path):
        self.path = root / "local.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    session_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    codex_home TEXT NOT NULL, member_name TEXT NOT NULL,
                    after_seq INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'active',
                    last_error TEXT, delivery_mode TEXT NOT NULL DEFAULT 'queue'
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    delivery_id TEXT NOT NULL, state TEXT NOT NULL,
                    error TEXT, PRIMARY KEY (session_id, seq)
                );
                CREATE TABLE IF NOT EXISTS relaunches (
                    source TEXT NOT NULL, target TEXT NOT NULL, state TEXT NOT NULL,
                    detail TEXT, at REAL NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(subscriptions)")}
            if "delivery_mode" not in columns:
                db.execute("ALTER TABLE subscriptions ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'queue'")
            if "member_system" not in columns:
                db.execute("ALTER TABLE subscriptions ADD COLUMN member_system TEXT")
            if "harness" not in columns:
                db.execute("ALTER TABLE subscriptions ADD COLUMN harness TEXT NOT NULL DEFAULT 'codex'")
            if "cwd" not in columns:
                db.execute("ALTER TABLE subscriptions ADD COLUMN cwd TEXT")
            if "hold_seq" not in columns:
                # 確かに届けられなかった返信の seq。これより新しい返信が来るまで、同じ返信を試し直さない。
                db.execute("ALTER TABLE subscriptions ADD COLUMN hold_seq INTEGER")
            columns = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            if "confirmed" not in columns:
                # 前からある行は確かめ済みとして扱う。キューへ入れた後の確かめは、新しい配送にだけ行う。
                db.execute("ALTER TABLE deliveries ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1")
            if "at" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN at REAL")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def add(self, session_id: str, thread_id: str, home: Path, member_name: str,
            delivery_mode: str = "queue", member_system: str | None = None,
            harness: str = "codex", cwd: str | None = None) -> None:
        if delivery_mode not in _MODES:
            raise ValueError(delivery_mode)
        if harness not in _HARNESSES:
            raise ValueError(harness)
        if member_system not in (None, *_REPLY_LABELS):
            raise ValueError(member_system)
        with self.connect() as db:
            db.execute("INSERT INTO subscriptions(session_id, thread_id, codex_home, member_name, delivery_mode, "
                       "member_system, harness, cwd) VALUES(?,?,?,?,?,?,?,?)",
                       (session_id, thread_id, str(home), member_name, delivery_mode, member_system, harness, cwd))

    def active(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT session_id FROM subscriptions WHERE state = 'active'")]

    def active_exec(self, thread_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM subscriptions WHERE state = 'active' AND delivery_mode = 'exec' "
                              "AND thread_id = ? ORDER BY session_id", (thread_id,)).fetchall()
        return [dict(row) for row in rows]

    def subscription(self, session_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM subscriptions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            raise DeliveryError("CALL_NOT_LOCAL", "この端末で開いた通話ではありません")
        return dict(row)

    def reserve(self, session_id: str, seq: int) -> tuple[str, str]:
        delivery_id = str(uuid.uuid5(_DELIVERY_NAMESPACE, f"{session_id}:{seq}"))
        with self.connect() as db:
            row = db.execute("SELECT state FROM deliveries WHERE session_id = ? AND seq = ?",
                             (session_id, seq)).fetchone()
            if row is None:
                db.execute("INSERT INTO deliveries(session_id,seq,delivery_id,state,at) VALUES(?,?,?,'sending',?)",
                           (session_id, seq, delivery_id, time.time()))
                return delivery_id, "new"
            if row["state"] == "failed":
                # 確かに届けられなかった返信は、新しい返信が来た時にもう一度試せる。
                db.execute("UPDATE deliveries SET state = 'sending', error = NULL WHERE session_id = ? AND seq = ?",
                           (session_id, seq))
                return delivery_id, "new"
            return delivery_id, row["state"]

    def reserved_at(self, session_id: str, seq: int) -> float:
        """この返信を最初に見た時刻。前の版が作った行には無いので、その時は今。"""
        with self.connect() as db:
            row = db.execute("SELECT at FROM deliveries WHERE session_id = ? AND seq = ?", (session_id, seq)).fetchone()
        return row["at"] if row is not None and row["at"] is not None else time.time()

    def submitted(self, session_id: str, seq: int, state: str = "submitted", confirmed: bool = True) -> None:
        """返信の扱いが決まった。confirmed が偽なら、キューが動いたかを後で確かめる。"""
        if state not in _DONE:
            raise ValueError(state)
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state = ?, error = NULL, confirmed = MIN(confirmed, ?), at = ? "
                       "WHERE session_id = ? AND seq = ?", (state, int(confirmed), time.time(), session_id, seq))
            db.execute("UPDATE subscriptions SET after_seq = MAX(after_seq, ?), last_error = NULL, hold_seq = NULL "
                       "WHERE session_id = ?", (seq, session_id))

    def advance(self, session_id: str, seq: int) -> None:
        """扱いが済んでいる返信の先へ進む。配送の行は変えない。"""
        with self.connect() as db:
            db.execute("UPDATE subscriptions SET after_seq = MAX(after_seq, ?), last_error = NULL WHERE session_id = ?",
                       (seq, session_id))

    def unconfirmed(self, session_id: str) -> list[dict[str, Any]]:
        """キューへ入れたが、動いたかをまだ確かめていない配送。"""
        with self.connect() as db:
            rows = db.execute("SELECT seq, delivery_id, at FROM deliveries WHERE session_id = ? "
                              "AND state = 'submitted' AND confirmed = 0 ORDER BY seq", (session_id,)).fetchall()
        return [dict(row) for row in rows]

    def confirm(self, session_id: str, seq: int, state: str = "submitted") -> None:
        if state not in _DONE:
            raise ValueError(state)
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state = ?, confirmed = 1 WHERE session_id = ? AND seq = ?",
                       (state, session_id, seq))

    def hold(self, session_id: str, seq: int, latest_seq: int, error: str) -> None:
        """確かに届けられなかった。通話の見張りは続け、これより新しい返信が来た時にもう一度試す。"""
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state = 'failed', error = ? WHERE session_id = ? AND seq = ?",
                       (error, session_id, seq))
            db.execute("UPDATE subscriptions SET hold_seq = ?, last_error = ? WHERE session_id = ?",
                       (latest_seq, error, session_id))

    def rewind(self, session_id: str, seq: int, error: str) -> None:
        """先へ進んだ後で、この返信がどこにも入っていないと分かった。新しい返信が来た時に、ここからやり直す。"""
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state = 'failed', error = ?, confirmed = 1 WHERE session_id = ? AND seq = ?",
                       (error, session_id, seq))
            db.execute("UPDATE subscriptions SET hold_seq = after_seq, after_seq = MIN(after_seq, ?), last_error = ? "
                       "WHERE session_id = ?", (seq - 1, error, session_id))

    def bound_to(self, harness: str, thread_id: str, session_id: str) -> list[dict[str, Any]]:
        """同じ会話に付いている、生きている通話。会話のIDが無い結び付けは、その通話だけ。"""
        with self.connect() as db:
            if thread_id:
                rows = db.execute("SELECT * FROM subscriptions WHERE state = 'active' AND harness = ? AND thread_id = ? "
                                  "ORDER BY session_id", (harness, thread_id)).fetchall()
            else:
                rows = db.execute("SELECT * FROM subscriptions WHERE session_id = ?", (session_id,)).fetchall()
        return [dict(row) for row in rows]

    def rebind(self, harness: str, thread_id: str, session_id: str, seat: str, cwd: str) -> int:
        """止まった会話に付いていた通話を、担当フォルダの席へ付け替える。付け替えた数を返す。"""
        with self.connect() as db:
            if thread_id:
                done = db.execute("UPDATE subscriptions SET thread_id = ?, delivery_mode = 'hosted', cwd = ? "
                                  "WHERE state = 'active' AND harness = ? AND thread_id = ? "
                                  "AND delivery_mode IN ('queue', 'hosted', 'manual')", (seat, cwd, harness, thread_id))
            else:
                done = db.execute("UPDATE subscriptions SET thread_id = ?, delivery_mode = 'hosted', cwd = ? "
                                  "WHERE session_id = ?", (seat, cwd, session_id))
            return done.rowcount

    def follow(self, thread_id: str, successor: str) -> int:
        """引き継ぎが済んだ Codex の会話に付いていた通話を、続きの会話へ付け替える。"""
        with self.connect() as db:
            return db.execute("UPDATE subscriptions SET thread_id = ? WHERE state = 'active' AND harness = 'codex' "
                              "AND delivery_mode = 'queue' AND thread_id = ?", (successor, thread_id)).rowcount

    def adopt(self, session_id: str, thread_id: str, home: Path, cwd: str | None) -> None:
        """新しい Codex の会話が、通話を自分へ付け替える。"""
        with self.connect() as db:
            db.execute("UPDATE subscriptions SET thread_id = ?, codex_home = ?, cwd = COALESCE(?, cwd), harness = 'codex', "
                       "delivery_mode = 'queue', state = 'active', last_error = NULL, hold_seq = NULL "
                       "WHERE session_id = ?", (thread_id, str(home), cwd, session_id))

    def note_relaunch(self, source: str, target: str, state: str, detail: str | None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO relaunches(source, target, state, detail, at) VALUES(?,?,?,?,?)",
                       (source, target, state, detail, time.time()))

    def relaunches(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT source, target, state, detail, at FROM relaunches ORDER BY rowid DESC LIMIT ?",
                              (limit,)).fetchall()
        return [dict(row) for row in rows]

    def defer(self, session_id: str, seq: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state = 'waiting', error = NULL WHERE session_id = ? AND seq = ?",
                       (session_id, seq))

    def stop(self, session_id: str, state: str, error: str | None = None, seq: int | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE subscriptions SET state = ?, last_error = ? WHERE session_id = ?",
                       (state, error, session_id))
            if seq is not None:
                db.execute("UPDATE deliveries SET state = ?, error = ? WHERE session_id = ? AND seq = ?",
                           (state, error, session_id, seq))

    def transport_error(self, session_id: str, detail: str | None) -> None:
        with self.connect() as db:
            db.execute("UPDATE subscriptions SET last_error = ? WHERE session_id = ?", (detail, session_id))

    def status(self, session_id: str) -> dict[str, Any]:
        subscription = self.subscription(session_id)
        with self.connect() as db:
            rows = db.execute("SELECT seq, delivery_id, state, error FROM deliveries WHERE session_id = ? ORDER BY seq",
                              (session_id,)).fetchall()
        return {"state": subscription["state"], "after_seq": subscription["after_seq"],
                "last_error": subscription["last_error"], "harness": subscription["harness"],
                "delivery_mode": subscription["delivery_mode"], "conversation": subscription["thread_id"],
                "deliveries": [dict(row) for row in rows]}

    async def delivery_status(self, session_id: str) -> dict[str, Any]:
        """キューの受付後に hook が取り出し中・中断した配送を、その状態で示す。"""
        subscription = self.subscription(session_id)
        status = self.status(session_id)
        for delivery in status["deliveries"]:
            if delivery["state"] == "submitted":
                hook_state = await hook_delivery_state(subscription["thread_id"], Path(subscription["codex_home"]),
                                                       delivery["delivery_id"])
                if hook_state:
                    delivery["state"] = hook_state
                    delivery["error"] = "CODEX_HOOK_DELIVERY_UNCONFIRMED" if hook_state == "unknown" else None
        return status


class Watchers:
    def __init__(self, store: LocalStore, receiver: bool = False):
        self.store = store
        # 常駐の受け取り係か。自分で取りに来る会話の通話は、その会話の MCP が鍵を持っている間は会話が生きている。
        # 受け取り係が鍵を取れた時は、会話が終わっている。
        self.receiver = receiver
        self.tasks: dict[str, asyncio.Task[None]] = {}
        # 会話の状態を見直す時刻（通話ごと）。見直すたびに App Server を起こすので、間を空ける。
        self.recheck_at: dict[str, float] = {}

    def start(self, session_id: str, claimed: int | None = None) -> None:
        """claimed は、通話を開いた会話が先に取っておいた鍵。"""
        if session_id in self.tasks:
            if claimed is not None:
                os.close(claimed)
            return
        task = asyncio.create_task(self.watch(session_id, claimed))
        self.tasks[session_id] = task
        task.add_done_callback(lambda done: self._finished(session_id, done))

    def _finished(self, session_id: str, task: asyncio.Task[None]) -> None:
        self.tasks.pop(session_id, None)
        self.recheck_at.pop(session_id, None)
        if not task.cancelled() and task.exception() is not None:
            log.exception("reply watch failed for %s", session_id, exc_info=task.exception())

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def watch(self, session_id: str, claimed: int | None = None) -> None:
        fd = claimed if claimed is not None else _claim_session(session_id)
        while fd is None:
            await asyncio.sleep(_POLL_SECONDS)
            if self.store.subscription(session_id)["state"] != "active":
                return
            fd = _claim_session(session_id)
        try:
            await self._watch_claimed(session_id)
        finally:
            os.close(fd)

    async def _watch_claimed(self, session_id: str) -> None:
        token_failures = 0
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                # 結び付けは、同じ会話に付いたほかの通話の見張りが付け替える事がある。毎回読み直す。
                subscription = self.store.subscription(session_id)
                if subscription["state"] != "active":
                    return
                # Read the token per poll so a rotated auth.json reaches a long-lived watcher.
                # A replacement in progress can leave it briefly unreadable; only a lasting
                # failure stops the watch.
                try:
                    headers = _headers()
                except (DeliveryError, ValueError, OSError) as exc:
                    token_failures += 1
                    detail = str(exc) if isinstance(exc, DeliveryError) else f"LOCAL_CONFIG_INVALID: {exc}"
                    if token_failures >= _TOKEN_READ_ATTEMPTS:
                        self.store.stop(session_id, "failed", detail)
                        return
                    self.store.transport_error(session_id, detail)
                    log.warning("reply watch token unreadable for %s: %s", session_id, exc)
                    await asyncio.sleep(_POLL_SECONDS)
                    continue
                token_failures = 0
                params: dict[str, Any] = {"party": "local", "after_seq": subscription["after_seq"]}
                if subscription["delivery_mode"] == "manual":
                    # 自分で取りに来る会話の代わりに読むだけ。既読の印は、会話が取りに来た時に付く。
                    params["peek"] = "1"
                try:
                    response = await client.get(_rest_url(session_id), headers=headers, params=params)
                    response.raise_for_status()
                    value = response.json()
                    if not isinstance(value, dict) or value.get("ok") is not True or not isinstance(value.get("messages"), list):
                        raise DeliveryError("BRIDGE_POLL_INVALID", "通話の受信応答が不正です")
                    self.store.transport_error(session_id, None)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code < 500:
                        self.store.stop(session_id, "failed", f"BRIDGE_POLL_HTTP_{exc.response.status_code}")
                        return
                    self.store.transport_error(session_id, f"BRIDGE_POLL_HTTP_{exc.response.status_code}")
                    log.warning("reply watch server error for %s: %s", session_id, exc)
                    await asyncio.sleep(_POLL_SECONDS)
                    continue
                except httpx.RequestError as exc:
                    self.store.transport_error(session_id, "BRIDGE_POLL_TRANSPORT")
                    log.warning("reply watch transport error for %s: %s", session_id, exc)
                    await asyncio.sleep(_POLL_SECONDS)
                    continue
                except (DeliveryError, json.JSONDecodeError) as exc:
                    self.store.stop(session_id, "failed", str(exc))
                    return
                messages = value["messages"]
                for message in messages:
                    if (not isinstance(message, dict) or not isinstance(message.get("seq"), int)
                            or message["seq"] <= subscription["after_seq"] or not isinstance(message.get("message"), str)):
                        self.store.stop(session_id, "failed", "BRIDGE_MESSAGE_INVALID")
                        return
                hold = subscription["hold_seq"]
                if messages and (hold is None or messages[-1]["seq"] > hold) and self._due(session_id):
                    report = _Reporter(client, headers, session_id)
                    for message in messages:
                        outcome = await self._handle(subscription, message, messages[-1]["seq"], report)
                        if outcome == "stop":
                            return
                        if outcome != "next":
                            break
                        subscription["after_seq"] = message["seq"]
                await self._confirm_queued(session_id, _Reporter(client, headers, session_id))
                if value.get("status") == "hungup" and not self.store.unconfirmed(session_id):
                    # 終わった通話に新しい返信は来ない。渡し終えたか、届けられないと決まった時に見張りを閉じる。
                    current = self.store.subscription(session_id)
                    if current["state"] != "active":
                        return
                    if not messages or current["after_seq"] >= messages[-1]["seq"] or current["hold_seq"] is not None:
                        self.store.stop(session_id, "closed", current["last_error"])
                        return
                await asyncio.sleep(_POLL_SECONDS)

    def _due(self, session_id: str) -> bool:
        return time.monotonic() >= self.recheck_at.get(session_id, 0.0)

    def _later(self, session_id: str) -> str:
        self.recheck_at[session_id] = time.monotonic() + _RECHECK_SECONDS
        return "wait"

    async def _handle(self, subscription: dict[str, Any], message: dict[str, Any], latest_seq: int,
                      report: "_Reporter") -> str:
        """返信を1通扱う。next＝次へ進む、wait＝後でもう一度見る、stop＝見張りを止めた。"""
        session_id, seq = subscription["session_id"], message["seq"]
        delivery_id, state = self.store.reserve(session_id, seq)
        if state in _DONE:
            self.store.advance(session_id, seq)
            return "next"
        if state not in ("new", "waiting"):
            self.store.stop(session_id, "unknown", "DELIVERY_PREVIOUSLY_STARTED", seq)
            await report(seq, "unknown", "DELIVERY_PREVIOUSLY_STARTED", None)
            return "stop"
        text = reply_text(subscription, seq, message["message"])
        try:
            mode = subscription["delivery_mode"]
            if mode == "manual":
                return await self._manual(subscription, message, text, report)
            if mode == "hosted":
                return await self._hosted(subscription, seq, text, report)
            return await self._queue(subscription, seq, delivery_id, text, report)
        except DeliveryError as exc:
            if exc.code == "RELAUNCH_BUSY":
                self.store.defer(session_id, seq)
                return "wait"
            if exc.outcome_unknown:
                self.store.stop(session_id, "unknown", str(exc), seq)
                log.error("reply delivery unknown for %s seq=%s: %s", session_id, seq, exc)
                await report(seq, "unknown", str(exc), None)
                return "stop"
            # 確かに届けていない。通話の見張りは続け、次の返信が来た時にもう一度試す。
            self.store.hold(session_id, seq, latest_seq, str(exc))
            log.error("reply delivery failed for %s seq=%s: %s", session_id, seq, exc)
            await report(seq, "failed", str(exc), None)
            return "wait"

    async def _queue(self, subscription: dict[str, Any], seq: int, delivery_id: str, text: str,
                     report: "_Reporter") -> str:
        """Codex の会話の公式キューへ入れる。止まっている会話なら、続きの会話か担当フォルダの席へ渡す。"""
        session_id, home = subscription["session_id"], Path(subscription["codex_home"])
        found = await codex_conversation(subscription["thread_id"], home)
        if not found.exists or found.turn == "interrupted":
            following = talk.successor(subscription["thread_id"], await talk.handoff_operations())
            if following:
                # Throughline が続きの会話へ乗り換えている。同じ会話に付いた通話ごと、そこへ付け替える。
                self.store.follow(subscription["thread_id"], following)
                return "wait"
            if found.exists and found.idle_seconds < _INTERRUPT_SETTLE_SECONDS:
                self.store.defer(session_id, seq)
                return self._later(session_id)
            reason = "途中で止められていて、続きの会話がありません" if found.exists else "会話が見つかりません"
            return await self._hand_over({**subscription, "cwd": subscription["cwd"] or found.cwd}, seq, [text],
                                         reason, report)
        receipt = await submit_reply(subscription["thread_id"], home, delivery_id, text)
        if receipt == "deferred":
            self.store.defer(session_id, seq)
            return "wait"
        injected = receipt == "injected"
        self.store.submitted(session_id, seq, "injected" if injected else "submitted", confirmed=injected)
        await report(seq, "submitted", None, f"codex:{subscription['thread_id']}")
        return "next"

    async def _hosted(self, subscription: dict[str, Any], seq: int, text: str, report: "_Reporter") -> str:
        """担当フォルダの席へ送る。席が動いていなければ、同じ名前で立て直す。"""
        seat = subscription["thread_id"]
        async with aiterm.connect() as terminal:
            if seat in await terminal.sessions():
                await terminal.send(seat, text)
                self.store.submitted(subscription["session_id"], seq)
                await report(seq, "submitted", None, f"hosted:{seat}")
                return "next"
        return await self._hand_over(subscription, seq, [text], "席が動いていませんでした", report)

    async def _manual(self, subscription: dict[str, Any], message: dict[str, Any], text: str,
                      report: "_Reporter") -> str:
        """自分で取りに来る会話。待っても取りに来なければ、担当フォルダの席へ渡す。"""
        session_id, seq = subscription["session_id"], message["seq"]
        if message.get("fetched") is True:
            self.store.submitted(session_id, seq, "fetched")
            await report(seq, "fetched", None, None)
            return "next"
        if not self.receiver:
            # この MCP を起こした会話が生きている。返信は会話が call_poll で取りに来る。
            self.store.defer(session_id, seq)
            return self._later(session_id)
        return await self._hand_over(subscription, seq, [text], "通話を開いた会話が終わっていました", report)

    async def _hand_over(self, subscription: dict[str, Any], seq: int, texts: list[str], reason: str,
                         report: "_Reporter") -> str:
        seat = await relaunch.hand_over(self.store, subscription, texts, reason)
        self.store.submitted(subscription["session_id"], seq, "relaunched")
        await report(seq, "relaunched", reason, f"hosted:{seat}")
        return "next"

    async def _confirm_queued(self, session_id: str, report: "_Reporter") -> None:
        """キューへ入れた返信が動いたかを確かめる。動かないキューに残った返信は取り消して、席へ渡す。"""
        waiting = [row for row in self.store.unconfirmed(session_id)
                   if time.time() - (row["at"] or 0.0) >= _CONFIRM_AFTER_SECONDS]
        if not waiting or not self._due(session_id):
            return
        subscription = self.store.subscription(session_id)
        if subscription["delivery_mode"] != "queue":
            # 付け替えが済んでいる。前の会話のキューは、もうこの通話の物ではない。
            for row in waiting:
                self.store.confirm(session_id, row["seq"])
            return
        thread_id, home = subscription["thread_id"], Path(subscription["codex_home"])
        seq = waiting[0]["seq"]
        try:
            for row in waiting:
                seq, delivery_id = row["seq"], row["delivery_id"]
                queued, found = await codex_queued(thread_id, home, delivery_id)
                if not queued:
                    self.store.confirm(session_id, seq)
                    await report(seq, "started", None, f"codex:{thread_id}")
                    continue
                if found.turn == "running" or (found.turn == "interrupted"
                                               and found.idle_seconds < _INTERRUPT_SETTLE_SECONDS):
                    self._later(session_id)  # 番の終わりにキューが渡す。中断の直後は、続きの会話を待つ
                    return
                # 寝たまま起きない・途中で止められた。このキューは人が入力するまで動かない。
                withdrawn = await codex_withdraw(thread_id, home, delivery_id)
                if withdrawn == "gone":  # 見ている間に番になった
                    self.store.confirm(session_id, seq)
                    await report(seq, "started", None, f"codex:{thread_id}")
                    continue
                if withdrawn != "withdrawn":
                    raise DeliveryError("CODEX_QUEUE_WITHDRAW_UNCONFIRMED", "キューからの取り消しを確かめられません",
                                        outcome_unknown=True)
                text = reply_text(subscription, seq, await fetch_body(session_id, seq))
                following = talk.successor(thread_id, await talk.handoff_operations())
                if following:
                    self.store.follow(thread_id, following)
                    await submit_reply(following, home, delivery_id, text)
                    self.store.submitted(session_id, seq, "submitted", confirmed=False)
                    await report(seq, "submitted", "引き継ぎ先の会話へ入れ直しました", f"codex:{following}")
                    return
                reason = ("途中で止められていて、続きの会話がありません" if found.turn == "interrupted"
                          else "会話が寝たままで、起こせませんでした")
                seat = await relaunch.hand_over(self.store, {**subscription, "cwd": subscription["cwd"] or found.cwd},
                                                [text], reason)
                self.store.confirm(session_id, seq, "relaunched")
                await report(seq, "relaunched", reason, f"hosted:{seat}")
                return
        except DeliveryError as exc:
            if exc.code == "RELAUNCH_BUSY":
                return
            log.error("queued reply for %s seq=%s could not be confirmed: %s", session_id, seq, exc)
            if exc.outcome_unknown:
                self.store.stop(session_id, "unknown", str(exc), seq)
                await report(seq, "unknown", str(exc), None)
                return
            # キューから取り消した後で、席へも入らなかった。次の返信が来た時に、この返信からもう一度渡す。
            self.store.rewind(session_id, seq, str(exc))
            await report(seq, "failed", str(exc), None)


class _Reporter:
    """端末の側が返信をどう扱ったかを、通話のサーバーへ知らせる。知らせの失敗では配送を止めない。"""

    def __init__(self, client: httpx.AsyncClient, headers: dict[str, str], session_id: str):
        self.client, self.headers, self.session_id = client, headers, session_id
        self.url = _rest_url(session_id).removesuffix("/poll") + "/receipts"

    async def __call__(self, seq: int, state: str, detail: str | None, conversation: str | None) -> None:
        try:
            response = await self.client.post(self.url, headers=self.headers, json={
                "seq": seq, "state": state, "detail": detail, "conversation": conversation})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("receipt for %s seq=%s was not stored: %s", self.session_id, seq, exc)


store = LocalStore(state_root())
watchers = Watchers(store)


def _launch_exec_watcher(session_id: str) -> None:
    directory = state_root() / "workers"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = directory / f"{session_id}.log"
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL,
                               "env": {**os.environ, "CALL_BRIDGE_STATE": str(state_root())}}
    if os.name == "nt":
        options["creationflags"] = (subprocess.DETACHED_PROCESS |
                                    subprocess.CREATE_NEW_PROCESS_GROUP |
                                    subprocess.CREATE_BREAKAWAY_FROM_JOB)
    else:
        options["start_new_session"] = True
    try:
        with open(log_path, "ab") as log_file:
            subprocess.Popen([sys.executable, "-m", "call_bridge.exec_watcher", session_id],
                             stdout=log_file, stderr=log_file, **options)
    except OSError as exc:
        raise DeliveryError("LOCAL_WATCHER_UNAVAILABLE", "通話の受信プロセスを起動できません") from exc


def _start_watch(session_id: str, claimed: int | None = None) -> None:
    subscription = store.subscription(session_id)
    mode = subscription["delivery_mode"]
    # exec の親へは、次のプロンプトの hook が渡す。自分で取りに来る会話の通話は、開いた会話と受け取り係だけが見る。
    if mode in ("queue", "hosted") or (mode == "manual" and (claimed is not None or watchers.receiver)):
        watchers.start(session_id, claimed)
    elif claimed is not None:
        os.close(claimed)


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    for session_id in store.active():
        _start_watch(session_id)
    try:
        yield
    finally:
        await watchers.close()


mcp = FastMCP("grokbot-bridge-local", instructions=(
    "call_open は、GrokBot または BellTeam の返信をこの端末で受け取ります。"
    "親が Codex の時は、返信を親のタスクへ自動で渡します。親は call_poll や待機ループを実行せず、作業を続けるかターンを終えてください。"
    "親が Claude Code・Cursor・Grok の時は、自動では渡しません。call_open の結果の parent_delivery.state が manual なら、"
    "call_poll で取りに来てください。会話が終わった後に届いた返信は、同じフォルダの席（Aiterm）へ渡されます。"
    "通話を引き継いだ時は、call_history で前のやりとりを読めます。"
), lifespan=_lifespan)


def _harness(name: str | None) -> str | None:
    """MCP の clientInfo.name から、親のハーネスを決める。"""
    if name == "codex-mcp-client":
        return "codex"
    if name == "claude-code":
        return "claude-code"
    if name is None:
        return None
    if name == "cursor-vscode" or name.startswith("cursor-vscode ") or name == "Cursor":
        return "cursor"
    return "grok" if "grok" in name.lower() else None


def _parent(ctx: Context) -> tuple[str, Path]:
    params = ctx.session.client_params
    name = params.clientInfo.name if params is not None else None
    meta = ctx.request_context.meta
    thread_id = (meta.model_extra or {}).get("threadId") if meta is not None else None
    if name != "codex-mcp-client" or not isinstance(thread_id, str):
        raise DeliveryError("PARENT_UNSUPPORTED", "Codex 親のタスクIDを取得できません")
    return thread_id, codex_home()


def _parent_harness(ctx: Context) -> str:
    params = ctx.session.client_params
    harness = _harness(params.clientInfo.name if params is not None else None)
    if harness is None:
        raise DeliveryError("PARENT_UNSUPPORTED", "親のハーネスを見分けられません（Codex・Claude Code・Cursor・Grok）")
    return harness


async def _parent_folder(ctx: Context) -> str:
    """親が作業しているフォルダ。親が roots を答える時はその先頭、答えない時はこの MCP が起こされた場所。"""
    try:
        roots = (await ctx.session.list_roots()).roots
    except Exception:  # noqa: BLE001 - roots に対応しない親は多い
        roots = []
    for root in roots:
        uri = str(root.uri)
        if uri.startswith("file://"):
            path = url2pathname(urlsplit(uri).path)
            if Path(path).is_dir():
                return str(Path(path))
    return os.getcwd()


@mcp.tool(description="電話帳を取得")
async def call_directory(query: str | None = None) -> dict[str, Any]:
    return await _remote_tool("call_directory", {"query": query})


@mcp.tool(description="GrokBot または BellTeam のメンバーとの通話を開く。親が Codex なら返信を自動で渡す。"
                      "Claude Code・Cursor・Grok は call_poll で取りに来る（parent_delivery.state=manual）")
async def call_open(local_id: str, local_label: str, member_name: str,
                    purpose: str | None = None,
                    member_system: Literal["grokbot", "bellteam"] = "grokbot",
                    local_system: Literal["local", "grokbot", "bellteam"] | None = None,
                    ctx: Context | None = None) -> dict[str, Any]:
    if ctx is None:
        raise DeliveryError("PARENT_UNAVAILABLE", "親タスクを確認できません")
    harness = _parent_harness(ctx)
    arguments = {
        "local_id": local_id, "local_label": local_label,
        "member_name": member_name, "purpose": purpose,
        "member_system": member_system,
    }
    # Omitted, the bridge uses the token's system (local for the shared token).
    if local_system is not None:
        arguments["local_system"] = local_system
    if harness != "codex":
        folder = await _parent_folder(ctx)
        result = await _remote_tool("call_open", arguments)
        session_id = _session_id(result)
        # 鍵を先に取ってから控えを作る。受け取り係は控えから通話を知るので、会話より先に鍵を取れない。
        claimed = _claim_session(session_id)
        store.add(session_id, "", Path(""), member_name, "manual", member_system, harness, folder)
        _start_watch(session_id, claimed)
        return {**result, "parent_delivery": {
            "state": "manual", "harness": harness, "folder": folder,
            "note": "返信は自動では届きません。call_poll で取りに来てください。この会話が終わった後に届いた返信は、同じフォルダの席へ渡されます。"}}
    thread_id, home = _parent(ctx)
    source = await verify_parent(thread_id, home)
    result = await _remote_tool("call_open", arguments)
    session_id = _session_id(result)
    store.add(session_id, thread_id, home, member_name,
              "exec" if source == "exec" else "queue", member_system, "codex", await _codex_folder(thread_id, home))
    _start_watch(session_id)
    state = "awaiting_parent_prompt" if source == "exec" else "watching"
    return {**result, "parent_delivery": {"state": state, "thread_id": thread_id}}


async def _codex_folder(thread_id: str, home: Path) -> str | None:
    """会話のフォルダ。席を立て直す時の場所になる。読めなくても通話は開く（立て直す時に会話から読み直す）。"""
    try:
        return (await codex_conversation(thread_id, home)).cwd
    except DeliveryError as exc:
        log.warning("conversation folder for %s was not read: %s", thread_id, exc)
        return None


def _session_id(result: dict[str, Any]) -> str:
    session_id = result.get("session_id")
    if not isinstance(session_id, str):
        raise DeliveryError("BRIDGE_RESPONSE_INVALID", "通話IDを確認できません")
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise DeliveryError("BRIDGE_RESPONSE_INVALID", "通話IDが不正です") from exc
    return session_id


@mcp.tool(description="この端末で開いた通話を、今の Codex のタスクへ付け替える。前の会話が止まった・引き継いだ時に、"
                      "新しい会話が呼ぶ。以後の返信はこのタスクへ届く。通話の session_id は変わらない")
async def call_adopt(session_id: str, ctx: Context | None = None) -> dict[str, Any]:
    if ctx is None:
        raise DeliveryError("PARENT_UNAVAILABLE", "親タスクを確認できません")
    thread_id, home = _parent(ctx)
    previous = store.subscription(session_id)
    if await verify_parent(thread_id, home) == "exec":
        raise DeliveryError("PARENT_UNSUPPORTED", "codex exec のタスクへは付け替えられません")
    store.adopt(session_id, thread_id, home, await _codex_folder(thread_id, home))
    _start_watch(session_id)
    return {"session_id": session_id, "previous": {"harness": previous["harness"], "conversation": previous["thread_id"],
                                                   "delivery_mode": previous["delivery_mode"], "state": previous["state"]},
            "parent_delivery": {"state": "watching", "thread_id": thread_id}}


@mcp.tool(description="通話へメッセージを送信。local は返信依頼が既定。返信不要なら reply_required=false")
async def call_send(session_id: str, from_party: Literal["local", "member"], message: str,
                    reply_required: bool = True) -> dict[str, Any]:
    return await _remote_tool("call_send", {"session_id": session_id, "from_party": from_party,
                                            "message": message, "reply_required": reply_required})


@mcp.tool(description="自分宛のメッセージを手動取得。自動配送中の親は通常不要")
async def call_poll(session_id: str, party: Literal["local", "member"], after_seq: int = 0) -> dict[str, Any]:
    return await _remote_tool("call_poll", {"session_id": session_id, "party": party, "after_seq": after_seq})


@mcp.tool(description="通話の両方の発言を seq の順に読む（既読にしない）。引き継いだ通話の、前のやりとりを読む時に使う")
async def call_history(session_id: str, after_seq: int = 0, limit: int = 50) -> dict[str, Any]:
    return await _remote_tool("call_history", {"session_id": session_id, "after_seq": after_seq, "limit": limit})


@mcp.tool(description="通話一覧を取得")
async def call_list(party: str | None = None, local_id: str | None = None,
                    member_name: str | None = None, status: str | None = None) -> dict[str, Any]:
    return await _remote_tool("call_list", {"party": party, "local_id": local_id,
                                            "member_name": member_name, "status": status})


@mcp.tool(description="通話を終了")
async def call_hangup(session_id: str, by_party: Literal["local", "member", "ops"],
                      reason: str | None = None) -> dict[str, Any]:
    return await _remote_tool("call_hangup", {"session_id": session_id,
                                              "by_party": by_party, "reason": reason})


@mcp.tool(description="通話の情報を取得")
async def call_info(session_id: str) -> dict[str, Any]:
    result = await _remote_tool("call_info", {"session_id": session_id})
    try:
        result["parent_delivery"] = await store.delivery_status(session_id)
    except DeliveryError as exc:
        if exc.code != "CALL_NOT_LOCAL":
            raise
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
