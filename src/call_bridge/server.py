"""grokbot-bridge MCP server — shared phone-call bridge for Grok Bot.

Streamable HTTP (FastMCP). Local agents and Grok Bot members both connect as MCP clients.
A switchboard agent wakes the member; conversation bodies go through this MCP, not the switchboard.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .db import CallStore
from .directory import DIRECTORY_HOP_HEADER, search_directory
from .wake import notify_wake

log = logging.getLogger("call_bridge")

_INSTRUCTIONS = """
# grokbot-bridge

共有の通話直通 MCP（Grok Bot 向け）。ローカル開発エージェントと Grok Bot メンバーが
同じサーバに MCP クライアントとして接続し、メッセージをやり取りする。
電話番（スイッチボード）エージェントは呼び出し（起こし）のみ。本文は中継しない。

## 流れ
1. ローカルが call_open でセッション作成（status=ringing）。サーバは設定済みならスイッチボード webhook へ wake を POST する（本文は含めない）
2. 電話番が相手メンバーを起こし、session_id と MCP URL を渡す
3. 両者 call_send / call_poll で会話
4. call_hangup で終了

## ツール
- call_directory … 電話帳。要求のたびに席プロフィールから組み立てる（UNIX ソケット優先。定期同期や手動 push は不要）
- call_open 以降 / call_open / call_send / call_poll / call_list / call_hangup / call_info
- from_party / party は 'local' または 'member'
- local の call_send は返信依頼を本文に付ける。返信不要の通知だけ reply_required=false を指定する
"""

Party = Literal["local", "member"]
HangupParty = Literal["local", "member", "ops"]

# Load .env early (compose also injects via env_file)
load_dotenv()

DB_PATH = os.environ.get("CALL_BRIDGE_DB", "data/calls.db")
HOST = os.environ.get("CALL_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CALL_BRIDGE_PORT", "18910"))
TOKEN = os.environ.get("CALL_BRIDGE_TOKEN", "").strip()

store = CallStore(DB_PATH)

mcp = FastMCP(
    "grokbot-bridge",
    instructions=_INSTRUCTIONS,
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
)


# ---------- MCP tools ----------


def _session_view(sess: dict[str, Any], wake: dict[str, str]) -> dict[str, Any]:
    """Session fields plus a non-fatal wake result. No message bodies."""
    return {
        "session_id": sess["session_id"],
        "status": sess["status"],
        "local_id": sess["local_id"],
        "local_label": sess["local_label"],
        "member_name": sess["member_name"],
        "purpose": sess["purpose"],
        "created_at": sess["created_at"],
        "wake": wake,
    }


@mcp.tool(
    description=(
        "通話セッションを開く（local→member）。status=ringing で開始。"
        "設定されていればスイッチボードへ wake 通知（本文なし）を送る。session_id を返す。"
    )
)
def call_open(
    local_id: str,
    local_label: str,
    member_name: str,
    purpose: str | None = None,
) -> dict[str, Any]:
    sess = store.open_session(local_id, local_label, member_name, purpose)
    return _session_view(sess, notify_wake(sess))


@mcp.tool(description="セッションへメッセージ送信。local は返信依頼が既定。返信不要なら reply_required=false。")
def call_send(session_id: str, from_party: Party, message: str,
              reply_required: bool = True) -> dict[str, Any]:
    try:
        return store.send_message(session_id, from_party, message, reply_required)
    except KeyError as e:
        return {"error": "not_found", "detail": str(e)}
    except (ValueError, RuntimeError) as e:
        return {"error": "rejected", "detail": str(e)}


@mcp.tool(
    description=(
        "自分宛の新着メッセージを取得。party は 'local' または 'member'。"
        "after_seq 以降の相手からのメッセージを返す（既定で delivered マーク）。"
    )
)
def call_poll(
    session_id: str,
    party: Party,
    after_seq: int = 0,
) -> dict[str, Any]:
    try:
        return store.poll_messages(session_id, party, after_seq=after_seq, mark_delivered=True)
    except KeyError as e:
        return {"error": "not_found", "detail": str(e)}
    except ValueError as e:
        return {"error": "rejected", "detail": str(e)}


@mcp.tool(description="アクティブ（またはフィルタした）セッション一覧。")
def call_list(
    party: str | None = None,
    local_id: str | None = None,
    member_name: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    sessions = store.list_sessions(
        party=party, local_id=local_id, member_name=member_name, status=status
    )
    return {"sessions": sessions, "count": len(sessions)}


@mcp.tool(description="通話終了。by_party は 'local' / 'member' / 'ops'。")
def call_hangup(
    session_id: str,
    by_party: HangupParty,
    reason: str | None = None,
) -> dict[str, Any]:
    try:
        sess = store.hangup(session_id, by_party, reason)
        return {
            "session_id": sess["session_id"],
            "status": sess["status"],
            "hangup_by": sess["hangup_by"],
            "hangup_reason": sess["hangup_reason"],
            "updated_at": sess["updated_at"],
        }
    except KeyError as e:
        return {"error": "not_found", "detail": str(e)}
    except ValueError as e:
        return {"error": "rejected", "detail": str(e)}


@mcp.tool(description="セッション詳細（メタデータ＋メッセージ数）。")
def call_info(session_id: str) -> dict[str, Any]:
    try:
        return store.session_info(session_id)
    except KeyError as e:
        return {"error": "not_found", "detail": str(e)}



@mcp.tool(
    description=(
        "電話帳。呼ぶたびに席プロフィール（name / title / description）を読む。"
        "優先順は CALL_BRIDGE_DIRECTORY_UNIX、CALL_BRIDGE_DIRECTORY_URL、"
        "ローカル agents の profile.json、最後に directory.json。"
        "ライブ応答は source=agent-profiles と agents_root。"
        "directory.json は source=directory.json の予備。query で部分一致。"
        "呼び出し可否フラグは無い。プロフィール更新は次の呼び出しから反映される。"
        "定期同期や編集後の push は不要。"
    )
)
def call_directory(query: str | None = None) -> dict[str, Any]:
    return search_directory(query)


# ---------- HTTP (non-MCP) ----------


def _check_bearer(request: Request) -> Response | None:
    """Return 401 Response if token required and missing/wrong; else None."""
    if not TOKEN:
        return None
    auth = request.headers.get("authorization", "")
    if auth == f"Bearer {TOKEN}":
        return None
    return JSONResponse(
        {"ok": False, "error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "service": "call-bridge",
            "status": "up",
            "version": "0.1.0",
        }
    )



@mcp.custom_route("/v0/directory", methods=["GET"])
async def rest_directory(request: Request) -> Response:
    denied = _check_bearer(request)
    if denied is not None:
        return denied
    query = request.query_params.get("q") or request.query_params.get("query")
    # Hop header: this GET is itself a directory fetch. Do not call the unix
    # socket or CALL_BRIDGE_DIRECTORY_URL again. Wake/webhook behavior is unchanged.
    skip_url = request.headers.get(DIRECTORY_HOP_HEADER, "").strip() == "1"
    return JSONResponse(search_directory(query, skip_url=skip_url))


@mcp.custom_route("/v0/sessions", methods=["POST"])
async def rest_open_session(request: Request) -> Response:
    denied = _check_bearer(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    local_id = body.get("local_id") or (body.get("guest") or {}).get("id")
    local_label = body.get("local_label") or (body.get("guest") or {}).get("label")
    member_name = body.get("member_name") or (body.get("to") or {}).get("name")
    purpose = body.get("purpose")
    if not local_id or not local_label or not member_name:
        return JSONResponse(
            {
                "ok": False,
                "error": "missing_fields",
                "need": ["local_id", "local_label", "member_name"],
            },
            status_code=400,
        )
    sess = store.open_session(str(local_id), str(local_label), str(member_name), purpose)
    wake = await asyncio.to_thread(notify_wake, sess)
    return JSONResponse({"ok": True, **_session_view(sess, wake)}, status_code=201)


@mcp.custom_route("/v0/sessions/{session_id}/messages", methods=["POST"])
async def rest_send(request: Request) -> Response:
    denied = _check_bearer(request)
    if denied is not None:
        return denied
    session_id = request.path_params["session_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    from_party = body.get("from_party")
    message = body.get("message")
    reply_required = body.get("reply_required", True)
    if from_party not in ("local", "member") or not message:
        return JSONResponse(
            {"ok": False, "error": "need from_party (local|member) and message"},
            status_code=400,
        )
    if not isinstance(reply_required, bool):
        return JSONResponse(
            {"ok": False, "error": "reply_required must be boolean"}, status_code=400
        )
    try:
        result = store.send_message(session_id, from_party, str(message), reply_required)
        return JSONResponse({"ok": True, **result})
    except KeyError as e:
        return JSONResponse({"ok": False, "error": "not_found", "detail": str(e)}, status_code=404)
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": "rejected", "detail": str(e)}, status_code=409)


@mcp.custom_route("/v0/sessions/{session_id}/poll", methods=["GET"])
async def rest_poll(request: Request) -> Response:
    denied = _check_bearer(request)
    if denied is not None:
        return denied
    session_id = request.path_params["session_id"]
    party = request.query_params.get("party", "")
    after_seq = int(request.query_params.get("after_seq", "0"))
    if party not in ("local", "member"):
        return JSONResponse(
            {"ok": False, "error": "query party=local|member required"},
            status_code=400,
        )
    try:
        result = store.poll_messages(session_id, party, after_seq=after_seq)
        return JSONResponse({"ok": True, **result})
    except KeyError as e:
        return JSONResponse({"ok": False, "error": "not_found", "detail": str(e)}, status_code=404)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require Bearer token on /mcp (and anything except /health).

    /v0/* also checks in-handler (custom routes skip FastMCP OAuth), but
    middleware covers /mcp streamable HTTP which has no built-in simple bearer.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/health" or path.rstrip("/") == "/health":
            return await call_next(request)
        # /v0 handlers do their own check so we don't double-gate, but
        # still protect /mcp and unknown paths when TOKEN is set.
        if path.startswith("/v0/"):
            return await call_next(request)
        if not TOKEN:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {TOKEN}":
            return JSONResponse(
                {"ok": False, "error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    if not TOKEN:
        log.warning(
            "CALL_BRIDGE_TOKEN unset — MCP and /v0 are open (dev only). "
            "Set a strong token in .env for production."
        )
    else:
        log.info("Bearer auth enabled (token length=%d)", len(TOKEN))

    mcp.settings.host = HOST
    mcp.settings.port = PORT

    # DNS-rebinding protection. Defaults are localhost-only; set
    # CALL_BRIDGE_ALLOWED_HOSTS / CALL_BRIDGE_ALLOWED_ORIGINS for public/LAN deploy.
    from mcp.server.transport_security import TransportSecuritySettings

    extra_hosts = [
        h.strip()
        for h in os.environ.get(
            "CALL_BRIDGE_ALLOWED_HOSTS",
            "127.0.0.1:*,localhost:*",
        ).split(",")
        if h.strip()
    ]
    extra_origins = [
        o.strip()
        for o in os.environ.get(
            "CALL_BRIDGE_ALLOWED_ORIGINS",
            "http://127.0.0.1:*,http://localhost:*",
        ).split(",")
        if o.strip()
    ]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=extra_hosts,
        allowed_origins=extra_origins,
    )

    log.info("Starting grokbot-bridge on http://%s:%s (Streamable HTTP /mcp)", HOST, PORT)

    import asyncio

    import uvicorn

    async def _serve() -> None:
        app = mcp.streamable_http_app()
        app.add_middleware(BearerAuthMiddleware)
        config = uvicorn.Config(
            app,
            host=HOST,
            port=PORT,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
