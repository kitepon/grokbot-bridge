"""通話の返信を裏で受信して親へ配送するローカル MCP。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import Context, FastMCP

from .codex_delivery import DeliveryError, codex_home, state_root, submit_reply, verify_parent

log = logging.getLogger("call_bridge.local")
_DELIVERY_NAMESPACE = uuid.UUID("ddf85db7-8d27-4c57-8ba5-9ad498cd64c9")
_POLL_SECONDS = 2.0


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


class LocalStore:
    def __init__(self, root: Path):
        self.path = root / "local.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    session_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    codex_home TEXT NOT NULL, member_name TEXT NOT NULL,
                    after_seq INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'active',
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    delivery_id TEXT NOT NULL, state TEXT NOT NULL,
                    error TEXT, PRIMARY KEY (session_id, seq)
                );
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def add(self, session_id: str, thread_id: str, home: Path, member_name: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO subscriptions(session_id, thread_id, codex_home, member_name) VALUES(?,?,?,?)",
                       (session_id, thread_id, str(home), member_name))

    def active(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT session_id FROM subscriptions WHERE state = 'active'")]

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
                db.execute("INSERT INTO deliveries(session_id,seq,delivery_id,state) VALUES(?,?,?,'sending')",
                           (session_id, seq, delivery_id))
                return delivery_id, "new"
            return delivery_id, row["state"]

    def submitted(self, session_id: str, seq: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state = 'submitted' WHERE session_id = ? AND seq = ?",
                       (session_id, seq))
            db.execute("UPDATE subscriptions SET after_seq = ?, last_error = NULL WHERE session_id = ?",
                       (seq, session_id))

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
                "last_error": subscription["last_error"], "deliveries": [dict(row) for row in rows]}


class Watchers:
    def __init__(self, store: LocalStore):
        self.store = store
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, session_id: str) -> None:
        if session_id in self.tasks:
            return
        task = asyncio.create_task(self.watch(session_id))
        self.tasks[session_id] = task
        task.add_done_callback(lambda done: self._finished(session_id, done))

    def _finished(self, session_id: str, task: asyncio.Task[None]) -> None:
        self.tasks.pop(session_id, None)
        if not task.cancelled() and task.exception() is not None:
            log.exception("reply watch failed for %s", session_id, exc_info=task.exception())

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def watch(self, session_id: str) -> None:
        fd = _claim_session(session_id)
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
        subscription = self.store.subscription(session_id)
        async with httpx.AsyncClient(headers=_headers(), timeout=10) as client:
            while True:
                try:
                    response = await client.get(_rest_url(session_id), params={
                        "party": "local", "after_seq": subscription["after_seq"],
                    })
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
                for message in value["messages"]:
                    if not isinstance(message, dict):
                        self.store.stop(session_id, "failed", "BRIDGE_MESSAGE_INVALID")
                        return
                    seq, body = message.get("seq"), message.get("message")
                    if not isinstance(seq, int) or seq <= subscription["after_seq"] or not isinstance(body, str):
                        self.store.stop(session_id, "failed", "BRIDGE_MESSAGE_INVALID")
                        return
                    delivery_id, state = self.store.reserve(session_id, seq)
                    if state == "submitted":
                        self.store.submitted(session_id, seq)
                    elif state != "new":
                        self.store.stop(session_id, "unknown", "DELIVERY_PREVIOUSLY_STARTED", seq)
                        return
                    else:
                        text = (f"GrokBot の返信です。session_id={session_id} seq={seq} "
                                f"member={subscription['member_name']}\n\n{body}")
                        try:
                            await submit_reply(subscription["thread_id"], Path(subscription["codex_home"]),
                                               delivery_id, text)
                        except DeliveryError as exc:
                            state = "unknown" if exc.outcome_unknown else "failed"
                            self.store.stop(session_id, state, str(exc), seq)
                            log.error("reply delivery %s for %s seq=%s: %s", state, session_id, seq, exc)
                            return
                        self.store.submitted(session_id, seq)
                    subscription["after_seq"] = seq
                if value.get("status") == "hungup":
                    self.store.stop(session_id, "closed")
                    return
                await asyncio.sleep(_POLL_SECONDS)


store = LocalStore(state_root())
watchers = Watchers(store)


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    for session_id in store.active():
        watchers.start(session_id)
    try:
        yield
    finally:
        await watchers.close()


mcp = FastMCP("grokbot-bridge-local", instructions=(
    "call_open は親 Codex のタスクへ GrokBot の返信を自動配送します。"
    "親は call_poll や待機ループを実行せず、作業を続けるかターンを終えてください。"
), lifespan=_lifespan)


def _parent(ctx: Context) -> tuple[str, Path]:
    params = ctx.session.client_params
    name = params.clientInfo.name if params is not None else None
    meta = ctx.request_context.meta
    thread_id = (meta.model_extra or {}).get("threadId") if meta is not None else None
    if name != "codex-mcp-client" or not isinstance(thread_id, str):
        raise DeliveryError("PARENT_UNSUPPORTED", "Codex 親のタスクIDを取得できません")
    return thread_id, codex_home()


@mcp.tool(description="電話帳を取得")
async def call_directory(query: str | None = None) -> dict[str, Any]:
    return await _remote_tool("call_directory", {"query": query})


@mcp.tool(description="GrokBot メンバーとの通話を開き、返信を親 Codex へ自動配送")
async def call_open(local_id: str, local_label: str, member_name: str,
                    purpose: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    if ctx is None:
        raise DeliveryError("PARENT_UNAVAILABLE", "親タスクを確認できません")
    thread_id, home = _parent(ctx)
    await verify_parent(thread_id, home)
    result = await _remote_tool("call_open", {
        "local_id": local_id, "local_label": local_label,
        "member_name": member_name, "purpose": purpose,
    })
    session_id = result.get("session_id")
    if not isinstance(session_id, str):
        raise DeliveryError("BRIDGE_RESPONSE_INVALID", "通話IDを確認できません")
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise DeliveryError("BRIDGE_RESPONSE_INVALID", "通話IDが不正です") from exc
    store.add(session_id, thread_id, home, member_name)
    watchers.start(session_id)
    return {**result, "parent_delivery": {"state": "watching", "thread_id": thread_id}}


@mcp.tool(description="通話へメッセージを送信")
async def call_send(session_id: str, from_party: Literal["local", "member"], message: str) -> dict[str, Any]:
    return await _remote_tool("call_send", {"session_id": session_id, "from_party": from_party, "message": message})


@mcp.tool(description="自分宛のメッセージを手動取得。自動配送中の親は通常不要")
async def call_poll(session_id: str, party: Literal["local", "member"], after_seq: int = 0) -> dict[str, Any]:
    return await _remote_tool("call_poll", {"session_id": session_id, "party": party, "after_seq": after_seq})


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
        result["parent_delivery"] = store.status(session_id)
    except DeliveryError as exc:
        if exc.code != "CALL_NOT_LOCAL":
            raise
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
