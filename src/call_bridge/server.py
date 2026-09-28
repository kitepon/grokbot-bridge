"""grokbot-bridge MCP server — shared phone-call bridge for Grok Bot.

Streamable HTTP (FastMCP). Local agents and Grok Bot members both connect as MCP clients.
A switchboard agent wakes the member. Local call_send posts session.message
(with the text) to that webhook so Marian can relay it into the member's main chat.
Member sends stay on this MCP.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth import UNAUTHENTICATED, AuthError, Authenticator, OPEN, Principal, check_open, is_participant, is_party
from .db import CallStore
from .deliver import dispatch_send
from .directory import DIRECTORY_HOP_HEADER, resolve_bellteam_member, resolve_member_agent_id, search_directory
from .wake import notify_wake

log = logging.getLogger("call_bridge")

_INSTRUCTIONS = """
# grokbot-bridge

共有の通話MCP。電話帳はGrokBotとBellTeamをsystemとidで区別する。
BellTeam宛てはcall_openのmember_system="bellteam"、member_nameには名前かBot IDを指定する。
BellTeamのBotが発信する時はlocal_system="bellteam"、local_idには自身のBot IDを指定する。
同名が複数あればBot IDを使う。local/memberは通話内の発信者/受信者を表す。
接続のトークンが所属に結び付いている時は、その所属（BellTeamはBot IDまで）の当事者としてだけ操作でき、違えばerror=forbiddenになる。
GrokBot宛てのsession.openedには本文を含めず、localのcall_sendをMarianが中継する。
BellTeam宛てはcall_sendがBellTeamへ直接届ける。マリアンは通らない。

## 流れ
1. localがcall_openでセッション作成（status=ringing）。GrokBot宛てだけスイッチボードへsession.openedをPOSTする（本文なし）
2. GrokBot宛ては電話番が相手を起こす。BellTeam宛ては最初のcall_sendで着信する
3. localのcall_sendは宛先の所属に応じて配送し、受付後に保存する。memberのcall_sendは保存し、BellTeam発信者へは返信を直接配送する
4. call_hangup で終了

## ツール
- call_directory … 電話帳。要求のたびに席プロフィールから組み立てる（UNIX ソケット優先。定期同期や手動 push は不要）
- call_open 以降 / call_open / call_send / call_poll / call_list / call_hangup / call_info
- from_party / party は 'local' または 'member'
- local の call_send は配送先の受付成功時に保存する。BellTeamへの送信後に受付結果を確認できなければdelivery.status=unknownで保存し、二重配送を避けるため自動再送しない。deliveredは受付を表し、相手の読了を表さない。返信不要の通知だけ reply_required=false
"""

Party = Literal["local", "member"]
HangupParty = Literal["local", "member", "ops"]

# Load .env early (compose also injects via env_file)
load_dotenv()

DB_PATH = os.environ.get("CALL_BRIDGE_DB", "data/calls.db")
HOST = os.environ.get("CALL_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CALL_BRIDGE_PORT", "18910"))
AUTH = Authenticator.from_env()

store = CallStore(DB_PATH)

mcp = FastMCP(
    "grokbot-bridge",
    instructions=_INSTRUCTIONS,
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
)


# ---------- MCP tools ----------


def _principal(ctx: Context | None) -> Principal:
    """The authenticated caller of this MCP request; in-process calls are open."""
    if ctx is None:
        return OPEN
    try:
        request = ctx.request_context.request
    except ValueError:
        return OPEN
    if request is None:
        return OPEN
    return _request_principal(request)


def _forbidden(detail: str) -> dict[str, Any]:
    return {"error": "forbidden", "detail": detail}


def _party_denied(principal: Principal, session_id: str, party: str) -> dict[str, Any] | None:
    sess = store.get_session(session_id)
    if sess is None or is_party(principal, sess, party):
        return None
    return _forbidden(f"this connection is not the {party} party of the call")


def _session_view(sess: dict[str, Any], wake: dict[str, str]) -> dict[str, Any]:
    """Session fields plus a non-fatal wake result. No message bodies."""
    return {
        "session_id": sess["session_id"],
        "status": sess["status"],
        "local_id": sess["local_id"],
        "local_label": sess["local_label"],
        "member_name": sess["member_name"],
        "member_system": sess.get("member_system", "grokbot"),
        "member_id": sess.get("member_id"),
        "local_system": sess.get("local_system", "local"),
        "purpose": sess["purpose"],
        "created_at": sess["created_at"],
        "wake": wake,
    }


@mcp.tool(
    description=(
        "通話セッションを開く（local→member）。BellTeam宛てはmember_system=bellteam、"
        "BellTeam Bot発信はlocal_system=bellteamと自身のBot IDをlocal_idに指定する。"
        "GrokBot宛てだけスイッチボードへ本文なしのwakeを送る。session_idを返す。"
    )
)
def call_open(
    local_id: str,
    local_label: str,
    member_name: str,
    purpose: str | None = None,
    member_system: str = "grokbot",
    local_system: str = "local",
    ctx: Context | None = None,
) -> dict[str, Any]:
    return _open(_principal(ctx), local_id, local_label, member_name, purpose, member_system, local_system)


def _open(
    principal: Principal,
    local_id: str,
    local_label: str,
    member_name: str,
    purpose: str | None,
    member_system: str,
    local_system: str,
) -> dict[str, Any]:
    if member_system not in ("grokbot", "bellteam") or local_system not in ("local", "grokbot", "bellteam"):
        return {"error": "rejected", "detail": "invalid member_system or local_system"}
    denied = check_open(principal, local_system, local_id)
    if denied:
        return _forbidden(denied)
    member_id = None
    if member_system == "bellteam":
        resolved = resolve_bellteam_member(member_name)
        if not resolved.get("ok"):
            return resolved
        member_id = resolved["id"]
        member_name = resolved["name"]
    else:
        resolved = resolve_member_agent_id(member_name)
        if not resolved.get("ok"):
            return resolved
        member_id = resolved["id"]
        member_name = resolved.get("name") or member_name
    sess = store.open_session(local_id, local_label, member_name, purpose, member_system, member_id, local_system)
    wake = notify_wake(sess) if member_system == "grokbot" else {"status": "skipped", "detail": "BellTeam direct delivery on call_send"}
    return _session_view(sess, wake)


@mcp.tool(
    description=(
        "セッションへメッセージ送信。localはGrokBot宛てならマリアンへ、BellTeam宛てなら直接配送する。"
        "返信不要なら reply_required=false。"
        "結果の delivery.status は delivered、error、unknown。"
        "BellTeamの結果がunknownなら本文を履歴へ保存し、二重配送を避けるため自動再送しない。"
        "確定した配送失敗時はlocalの本文を保存しない。memberの返信は保存し、BellTeam発信者へは直接届ける。"
    )
)
def call_send(session_id: str, from_party: Party, message: str,
              reply_required: bool = True, ctx: Context | None = None) -> dict[str, Any]:
    return _send(_principal(ctx), session_id, from_party, message, reply_required)


def _send(principal: Principal, session_id: str, from_party: str, message: str,
          reply_required: bool) -> dict[str, Any]:
    denied = _party_denied(principal, session_id, from_party)
    if denied:
        return denied
    try:
        return dispatch_send(store, session_id, from_party, message, reply_required)
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
    ctx: Context | None = None,
) -> dict[str, Any]:
    return _poll(_principal(ctx), session_id, party, after_seq)


def _poll(principal: Principal, session_id: str, party: str, after_seq: int) -> dict[str, Any]:
    denied = _party_denied(principal, session_id, party)
    if denied:
        return denied
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
    ctx: Context | None = None,
) -> dict[str, Any]:
    principal = _principal(ctx)
    sessions = store.list_sessions(
        party=party, local_id=local_id, member_name=member_name, status=status
    )
    sessions = [s for s in sessions if is_participant(principal, s)]
    return {"sessions": sessions, "count": len(sessions)}


@mcp.tool(description="通話終了。by_party は 'local' / 'member' / 'ops'。")
def call_hangup(
    session_id: str,
    by_party: HangupParty,
    reason: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    principal = _principal(ctx)
    if by_party == "ops":
        if not principal.ops:
            return _forbidden("this connection may not hang up as ops")
    else:
        denied = _party_denied(principal, session_id, by_party)
        if denied:
            return denied
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
def call_info(session_id: str, ctx: Context | None = None) -> dict[str, Any]:
    principal = _principal(ctx)
    try:
        info = store.session_info(session_id)
        if not is_participant(principal, info):
            return _forbidden("this connection is not a party of the call")
        return info
    except KeyError as e:
        return {"error": "not_found", "detail": str(e)}



@mcp.tool(
    description=(
        "電話帳。GrokBotのプロフィールとBellTeamのBot一覧を要求ごとに読む。"
        "各項目のsystemとidで所属と既存Bot IDを区別する。"
        "優先順は CALL_BRIDGE_DIRECTORY_UNIX、CALL_BRIDGE_DIRECTORY_URL、"
        "ローカル agents の profile.json、最後に directory.json。"
        "GrokBotのdirectory.jsonは予備。片方の取得失敗時も他方を返す。queryで部分一致。"
        "呼び出し可否フラグは無い。プロフィール更新は次の呼び出しから反映される。"
        "定期同期や編集後の push は不要。"
    )
)
def call_directory(query: str | None = None) -> dict[str, Any]:
    return search_directory(query)


# ---------- HTTP (non-MCP) ----------


def _delivery_http_status(error: str) -> int:
    if error in ("target_not_found", "not_found"):
        return 404
    if error == "webhook_not_configured":
        return 503
    if error == "unavailable":
        return 503
    if error == "empty":
        return 400
    if error == "rejected":
        return 409
    if error == "forbidden":
        return 403
    return 502


def _request_principal(request: Request) -> Principal:
    """The principal the auth middleware recorded; nothing matches without it."""
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return principal
    return UNAUTHENTICATED if AUTH.enabled else OPEN


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "service": "call-bridge",
            "status": "up",
            "version": "0.1.2",
        }
    )



@mcp.custom_route("/v0/directory", methods=["GET"])
async def rest_directory(request: Request) -> Response:
    query = request.query_params.get("q") or request.query_params.get("query")
    # Hop header: this GET is itself a directory fetch. Do not call the unix
    # socket or CALL_BRIDGE_DIRECTORY_URL again. Wake/webhook behavior is unchanged.
    skip_url = request.headers.get(DIRECTORY_HOP_HEADER, "").strip() == "1"
    return JSONResponse(search_directory(query, skip_url=skip_url))


@mcp.custom_route("/v0/sessions", methods=["POST"])
async def rest_open_session(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    local_id = body.get("local_id") or (body.get("guest") or {}).get("id")
    local_label = body.get("local_label") or (body.get("guest") or {}).get("label")
    member_name = body.get("member_name") or (body.get("to") or {}).get("name")
    purpose = body.get("purpose")
    member_system = body.get("member_system", "grokbot")
    local_system = body.get("local_system", "local")
    if not local_id or not local_label or not member_name:
        return JSONResponse(
            {
                "ok": False,
                "error": "missing_fields",
                "need": ["local_id", "local_label", "member_name"],
            },
            status_code=400,
        )
    result = await asyncio.to_thread(_open, _request_principal(request), str(local_id), str(local_label), str(member_name), purpose, member_system, local_system)
    if result.get("error"):
        return JSONResponse({"ok": False, **result}, status_code=_delivery_http_status(str(result["error"])))
    return JSONResponse({"ok": True, **result}, status_code=201)


@mcp.custom_route("/v0/sessions/{session_id}/messages", methods=["POST"])
async def rest_send(request: Request) -> Response:
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
        result = await asyncio.to_thread(
            _send,
            _request_principal(request),
            session_id,
            from_party,
            str(message),
            reply_required,
        )
    except KeyError as e:
        return JSONResponse({"ok": False, "error": "not_found", "detail": str(e)}, status_code=404)
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": "rejected", "detail": str(e)}, status_code=409)
    if result.get("error"):
        return JSONResponse(
            {"ok": False, **result},
            status_code=_delivery_http_status(str(result["error"])),
        )
    return JSONResponse({"ok": True, **result})


@mcp.custom_route("/v0/sessions/{session_id}/poll", methods=["GET"])
async def rest_poll(request: Request) -> Response:
    session_id = request.path_params["session_id"]
    party = request.query_params.get("party", "")
    after_seq = int(request.query_params.get("after_seq", "0"))
    if party not in ("local", "member"):
        return JSONResponse(
            {"ok": False, "error": "query party=local|member required"},
            status_code=400,
        )
    result = _poll(_request_principal(request), session_id, party, after_seq)
    if result.get("error"):
        return JSONResponse({"ok": False, **result}, status_code=_delivery_http_status(str(result["error"])))
    return JSONResponse({"ok": True, **result})


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate every path except /health and record the caller.

    The principal goes to ``request.state.principal``; MCP tools and /v0
    handlers read it to decide which party of a call the caller may act as.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/health" or path.rstrip("/") == "/health":
            return await call_next(request)
        try:
            principal = AUTH.authenticate(request.headers)
        except AuthError as e:
            return JSONResponse({"ok": False, "error": "forbidden", "detail": str(e)}, status_code=403)
        if principal is None:
            return JSONResponse(
                {"ok": False, "error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.principal = principal
        return await call_next(request)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    if not AUTH.enabled:
        log.warning(
            "CALL_BRIDGE_TOKEN and CALL_BRIDGE_TOKENS_FILE unset — MCP and /v0 are open (dev only). "
            "Set tokens for production."
        )
    else:
        log.info(
            "Bearer auth enabled (bound tokens=%d, legacy token=%s)",
            len(AUTH.entries), "on" if AUTH.legacy_token else "off",
        )

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
