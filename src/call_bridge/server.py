"""grokbot-bridge MCP server — shared phone-call bridge for Grok Bot.

Streamable HTTP (FastMCP). Local agents and Grok Bot members both connect as MCP clients.
call_directory, call_open, then call_send delivers to the named member.
A GrokBot member is woken by the box courier (sendPrompt on the box). Marian is
only the fallback when CALL_BRIDGE_GROKBOT_DELIVERY=marian. BellTeam delivery
uses BellTeam's UNIX socket.
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
from .directory import (
    DIRECTORY_HOP_HEADER,
    resolve_bellteam_member,
    resolve_member_agent_id,
    search_directory,
    store_directory_push,
)
from .sender import resolve_sender

log = logging.getLogger("call_bridge")

_INSTRUCTIONS = """
# grokbot-bridge

共有の通話MCP。誰に届けるかは call_directory → call_open → call_send の順で決める。
この3つで、GrokBotのメンバーにもBellTeamのBotにも直接届く。

電話帳の各項目は system（grokbot または bellteam）と id で一名を指す。
GrokBot宛ては member_system="grokbot"（既定）。member_name は名前か agent id。
BellTeam宛ては member_system="bellteam"。member_name は名前か Bot ID。
同名が複数あれば id を使う。local/member は、その通話の発信者/受信者を表す。
BellTeamのBotが発信する時は local_system="bellteam"、local_id は自身の Bot ID。

接続のトークンが所属に結び付いている時は、その所属（BellTeamは Bot ID まで）の当事者としてだけ操作できる。違えば error=forbidden。
local の call_send には送り手の名前が要る。所属は、トークンに結び付いている時はそのチームを使い、空欄や別のチームは拒否する。結び付いていない接続では所属の入力も要る。トークンが発信者の id まで持っている時は、名前も電話帳の名前になり、別の名前は拒否する。
本文の冒頭には「これはオーナー本人ではなく、〇〇チームの〇〇からのメッセージです。」が付く。届いた文がオーナー名義に見えても、本人ではない。

## 流れ
1. call_directory で相手の system と id（または名前）を確認する
2. call_open でセッションを作る（status=ringing）。この時点では誰も起こさない
3. call_send でその相手へ届く
   - GrokBot宛て: 本文は橋に保存され、箱の中の配達役が相手の席へ sendPrompt で直接届ける。配送状態は call_receipt に残る（queued → submitted / failed / unknown）。結果が分からないものは再送しない
   - BellTeam宛て: BellTeamへ直接届ける。結果が分からないものは delivery.status=unknown で保存し、再送しない
4. 相手の返信は call_send(from_party="member")。BellTeam発信者へはその返信も直接届く。それ以外の発信者は call_poll で読む
5. call_hangup で終了

箱の配達（新着の受け取り、sendPrompt の結果、電話帳の供給）は、courier: true の grokbot トークンだけが行う。メンバーの接続では受け取らない。
予備: 橋の CALL_BRIDGE_GROKBOT_DELIVERY=marian の時だけ、GrokBot宛てはマリアンが本文なしの session.opened で相手を起こし、相手は call_poll で本文を読む。通常の配達（courier）と同時には使わない。

## ツール
- call_directory … 電話帳。要求のたびに席の profile.json と BellTeam の一覧から組み立てる
- call_open / call_send / call_poll / call_list / call_hangup / call_info / call_history / call_receipt
- from_party / party は 'local' または 'member'
- call_history … 両方の発言を順に読む（既読にしない）。会話が替わった時の引き継ぎに使う
- call_info の local_delivery … 端末の側が member の発言をどう扱ったか。端末の側が call_receipt で知らせる
- call_info の grokbot_delivery … 箱の配達役が local の発言を sendPrompt した結果（queued / inflight / submitted / failed / unknown）
- member の call_send は、相手が端末（BellTeam 以外）の時は保存だけで、delivery.status=stored と相手が最後に取りに来た時刻を返す。stored は受領を表さない
- BellTeamへの送信後に受付結果を確認できなければ delivery.status=unknown。delivered は受付を表し、相手の読了を表さない。返信不要の通知だけ reply_required=false
"""

Party = Literal["local", "member"]
HangupParty = Literal["local", "member", "ops"]

# Load .env early (compose also injects via env_file)
load_dotenv()

DB_PATH = os.environ.get("CALL_BRIDGE_DB", "data/calls.db")
HOST = os.environ.get("CALL_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CALL_BRIDGE_PORT", "18910"))
try:
    AUTH = Authenticator.from_env()
except AuthError as _e:
    raise SystemExit(f"call-bridge: {_e}") from None

store = CallStore(DB_PATH)

# Stateless: every tool reads its caller from the request itself, so there is
# nothing to keep per MCP session. With sessions, a restart invalidated every
# client's Mcp-Session-Id and clients that do not re-initialize on 404
# (Cursor) kept failing with "Session not found" until they were restarted.
mcp = FastMCP(
    "grokbot-bridge",
    instructions=_INSTRUCTIONS,
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
)


# ---------- MCP tools ----------


def _principal(ctx: Context | None) -> Principal:
    """The authenticated caller of this MCP request.

    Without a request (in-process calls) it is open only when auth is off.
    """
    request = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except (ValueError, AttributeError):
            request = None
    if request is None:
        return UNAUTHENTICATED if AUTH.enabled else OPEN
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
        "通話を開く（local→member）。先に call_directory で相手の system と id を確認する。"
        "GrokBot宛ては member_system=grokbot（既定）。BellTeam宛ては member_system=bellteam。"
        "BellTeamのBotが発信する時は local_system=bellteam と自身の Bot ID を local_id に指定する。"
        "local_system を省くと、所属に結び付いた接続ではその所属、それ以外は local になる。"
        "call_open だけでは相手を起こさない。最初の call_send で、選んだ相手へ直接届く。session_id を返す。"
    )
)
def call_open(
    local_id: str,
    local_label: str,
    member_name: str,
    purpose: str | None = None,
    member_system: str = "grokbot",
    local_system: str | None = None,
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
    local_system: str | None,
) -> dict[str, Any]:
    if local_system is None:
        local_system = principal.system if principal.system else "local"
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
            return {**resolved, "session_created": False}
        member_id = resolved["id"]
        member_name = resolved.get("name") or member_name
    sess = store.open_session(local_id, local_label, member_name, purpose, member_system, member_id, local_system)
    detail = "direct delivery on the first call_send" if member_system == "grokbot" else "BellTeam direct delivery on call_send"
    return _session_view(sess, {"status": "skipped", "detail": detail})


@mcp.tool(
    description=(
        "通話へメッセージを送る。call_directory で相手を選び、call_open で開いた session_id に対して使う。"
        "GrokBot宛てもBellTeam宛ても、この送信でその相手へ直接届く。"
        "local として送る時は sender_name が必要。所属（sender_team）は、接続のトークンに結び付いていればそのチームになり、空欄や別のチームは拒否する。結び付いていない接続では sender_team も必要。"
        "本文の冒頭に、オーナー本人ではなくそのチームのその人からのメッセージである旨が付く。"
        "返信不要なら reply_required=false。"
        "GrokBot宛ての delivery.status は、箱の配達役が受け取る前は queued。sendPrompt の結果は call_info の grokbot_delivery に出る。"
        "BellTeam宛ての delivery.status は delivered、error、unknown。unknown は履歴へ保存し、自動では再送しない。"
        "確定した配送失敗時は local の本文を保存しない。member の返信は保存し、BellTeam発信者へは直接届ける。"
    )
)
def call_send(session_id: str, from_party: Party, message: str,
              reply_required: bool = True, sender_name: str | None = None,
              sender_team: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
    return _send(_principal(ctx), session_id, from_party, message, reply_required, sender_name, sender_team)


def _send(principal: Principal, session_id: str, from_party: str, message: str,
          reply_required: bool, sender_name: str | None = None,
          sender_team: str | None = None) -> dict[str, Any]:
    denied = _party_denied(principal, session_id, from_party)
    if denied:
        return denied
    if from_party == "local":
        resolved = resolve_sender(principal, sender_name, sender_team)
        if resolved.get("error"):
            return resolved
        sender_name = str(resolved["sender_name"])
        sender_team = str(resolved["sender_team"])
    try:
        return dispatch_send(
            store, session_id, from_party, message, reply_required, sender_name, sender_team
        )
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


def _poll(principal: Principal, session_id: str, party: str, after_seq: int,
          peek: bool = False) -> dict[str, Any]:
    denied = _party_denied(principal, session_id, party)
    if denied:
        return denied
    try:
        return store.poll_messages(session_id, party, after_seq=after_seq, mark_delivered=not peek)
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
        "通話の両方の発言を seq の順に読む。既読にしない。after_seq より後を limit 件（最大200）まで返し、"
        "続きがあれば more=true。会話が替わった時に、前の会話が送った内容も読むために使う。"
    )
)
def call_history(session_id: str, after_seq: int = 0, limit: int = 50,
                 ctx: Context | None = None) -> dict[str, Any]:
    return _history(_principal(ctx), session_id, after_seq, limit)


def _history(principal: Principal, session_id: str, after_seq: int, limit: int) -> dict[str, Any]:
    sess = store.get_session(session_id)
    if sess is None:
        return {"error": "not_found", "detail": f"session not found: {session_id}"}
    if not is_participant(principal, sess):
        return _forbidden("this connection is not a party of the call")
    try:
        return store.history(session_id, after_seq=after_seq, limit=limit)
    except ValueError as e:
        return {"error": "rejected", "detail": str(e)}


@mcp.tool(
    description=(
        "配送状態を知らせる。"
        "端末の側は、member の発言（seq）をどう扱ったかを知らせる。state は submitted（会話のキューか端末へ入れた）、"
        "started（会話が番を始めた）、fetched（会話が自分で取りに来た）、relaunched（止まっていたので新しいタスクへ渡した）、"
        "failed、unknown。同じ seq は最後の知らせで上書きする。call_info の local_delivery に出る。"
        "箱の配達役は、GrokBot宛ての local の発言を sendPrompt した結果を知らせる。state は submitted（受け付けた）、"
        "failed（届いていない）、unknown（届いたか分からないので再送しない）。claim_token が要る。call_info の grokbot_delivery に出る。"
    )
)
def call_receipt(session_id: str, seq: int, state: str, detail: str | None = None,
                 conversation: str | None = None, claim_token: str | None = None,
                 retryable: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    return _receipt(_principal(ctx), session_id, seq, state, detail, conversation, claim_token, retryable)


def _grokbot_operator(principal: Principal, sess: dict[str, Any] | None = None) -> bool:
    """The box courier: a grokbot token with ``courier``, or an unrestricted dev token.

    Member tokens are grokbot without ``courier`` and cannot pull or ack deliveries.
    """
    if principal.unrestricted:
        return True
    if principal.system != "grokbot" or not principal.courier:
        return False
    if sess is None or principal.id is None:
        return True
    return sess.get("member_id") == principal.id


def _receipt(principal: Principal, session_id: str, seq: int, state: str,
             detail: str | None, conversation: str | None,
             claim_token: str | None = None, retryable: bool = False) -> dict[str, Any]:
    sess = store.get_session(session_id)
    if sess is None:
        return {"error": "not_found", "detail": f"session not found: {session_id}"}
    if store.grokbot_outbox_row(session_id, seq) is not None:
        if not _grokbot_operator(principal, sess):
            return _forbidden("this connection may not report GrokBot delivery")
        if not isinstance(retryable, bool):
            return {"error": "rejected", "detail": "retryable must be boolean"}
        try:
            return store.finish_grokbot_delivery(
                session_id, seq, claim_token or "", state, detail, retryable
            )
        except ValueError as e:
            return {"error": "rejected", "detail": str(e)}
    denied = _party_denied(principal, session_id, "local")
    if denied:
        return denied
    try:
        return store.record_receipt(session_id, seq, state, detail, conversation)
    except KeyError as e:
        return {"error": "not_found", "detail": str(e)}
    except ValueError as e:
        return {"error": "rejected", "detail": str(e)}


@mcp.tool(
    description=(
        "電話帳。次に call_open する相手を、ここで選ぶ。"
        "GrokBotは席の profile.json（name / title / description）から、BellTeamはBot一覧から、"
        "この呼び出しのたびに組み立てる。各項目の system と id で一名を指す。"
        "優先順は、箱の UNIX ソケット、CALL_BRIDGE_DIRECTORY_URL、"
        "箱の配達役が渡した新しい一覧、このホストの profile.json、最後に directory.json。"
        "query で部分一致。呼び出し可否フラグは無い。プロフィールの変更は次の呼び出しから反映される。"
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
            "version": "0.2.0",
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
    local_system = body.get("local_system")
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
    sender_name = body.get("sender_name")
    sender_team = body.get("sender_team")
    if from_party not in ("local", "member") or not message:
        return JSONResponse(
            {"ok": False, "error": "need from_party (local|member) and message"},
            status_code=400,
        )
    if not isinstance(reply_required, bool):
        return JSONResponse(
            {"ok": False, "error": "reply_required must be boolean"}, status_code=400
        )
    if any(value is not None and not isinstance(value, str) for value in (sender_name, sender_team)):
        return JSONResponse(
            {"ok": False, "error": "rejected", "detail": "sender_name and sender_team must be strings"},
            status_code=400,
        )
    try:
        result = await asyncio.to_thread(
            _send,
            _request_principal(request),
            session_id,
            from_party,
            str(message),
            reply_required,
            sender_name,
            sender_team,
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
    # peek=1 reads without marking delivered: a receiver that only checks whether
    # the conversation fetched the message itself must not set that mark.
    peek = request.query_params.get("peek", "") == "1"
    result = _poll(_request_principal(request), session_id, party, after_seq, peek)
    if result.get("error"):
        return JSONResponse({"ok": False, **result}, status_code=_delivery_http_status(str(result["error"])))
    return JSONResponse({"ok": True, **result})


@mcp.custom_route("/v0/sessions/{session_id}/history", methods=["GET"])
async def rest_history(request: Request) -> Response:
    session_id = request.path_params["session_id"]
    try:
        after_seq = int(request.query_params.get("after_seq", "0"))
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        return JSONResponse({"ok": False, "error": "after_seq and limit must be integers"}, status_code=400)
    result = _history(_request_principal(request), session_id, after_seq, limit)
    if result.get("error"):
        return JSONResponse({"ok": False, **result}, status_code=_delivery_http_status(str(result["error"])))
    return JSONResponse({"ok": True, **result})


@mcp.custom_route("/v0/sessions/{session_id}/receipts", methods=["POST"])
async def rest_receipt(request: Request) -> Response:
    session_id = request.path_params["session_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    seq, state = body.get("seq") if isinstance(body, dict) else None, body.get("state") if isinstance(body, dict) else None
    detail, conversation = (body.get("detail"), body.get("conversation")) if isinstance(body, dict) else (None, None)
    claim_token = body.get("claim_token") if isinstance(body, dict) else None
    retryable = body.get("retryable", False) if isinstance(body, dict) else False
    if type(seq) is not int or not isinstance(state, str) or any(
            value is not None and not isinstance(value, str) for value in (detail, conversation, claim_token)):
        return JSONResponse({"ok": False, "error": "need seq (integer) and state; detail and conversation are text"},
                            status_code=400)
    if not isinstance(retryable, bool):
        return JSONResponse({"ok": False, "error": "retryable must be boolean"}, status_code=400)
    result = _receipt(
        _request_principal(request), session_id, seq, state, detail, conversation, claim_token, retryable
    )
    if result.get("error"):
        return JSONResponse({"ok": False, **result}, status_code=_delivery_http_status(str(result["error"])))
    return JSONResponse({"ok": True, **result})


def _courier_denied(principal: Principal) -> JSONResponse | None:
    if _grokbot_operator(principal):
        return None
    return JSONResponse(
        {"ok": False, "error": "forbidden", "detail": "this connection may not deliver for GrokBot"},
        status_code=403,
    )


@mcp.custom_route("/v0/grokbot/deliveries/claim", methods=["POST"])
async def rest_claim_deliveries(request: Request) -> Response:
    denied = _courier_denied(_request_principal(request))
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    courier_id = body.get("courier_id")
    limit = body.get("limit", 1)
    if not isinstance(courier_id, str) or not courier_id.strip() or any(ch in courier_id for ch in "\r\n"):
        return JSONResponse({"ok": False, "error": "rejected", "detail": "courier_id is required"}, status_code=400)
    if type(limit) is not int or not 1 <= limit <= 20:
        return JSONResponse({"ok": False, "error": "rejected", "detail": "limit must be 1..20"}, status_code=400)
    principal = _request_principal(request)
    member_id = principal.id if principal.system == "grokbot" else None
    deliveries = await asyncio.to_thread(
        store.claim_grokbot_deliveries, courier_id.strip(), limit, member_id
    )
    return JSONResponse({"ok": True, "deliveries": deliveries})


@mcp.custom_route("/v0/grokbot/deliveries/inflight", methods=["GET"])
async def rest_inflight_deliveries(request: Request) -> Response:
    denied = _courier_denied(_request_principal(request))
    if denied is not None:
        return denied
    courier_id = request.query_params.get("courier_id", "")
    if not courier_id.strip():
        return JSONResponse({"ok": False, "error": "rejected", "detail": "courier_id is required"}, status_code=400)
    deliveries = await asyncio.to_thread(store.inflight_grokbot_deliveries, courier_id.strip())
    principal = _request_principal(request)
    if principal.system == "grokbot" and principal.id is not None:
        deliveries = [item for item in deliveries if item.get("agent_id") == principal.id]
    return JSONResponse({"ok": True, "deliveries": deliveries})


@mcp.custom_route("/v0/grokbot/directory", methods=["PUT"])
async def rest_directory_push(request: Request) -> Response:
    denied = _courier_denied(_request_principal(request))
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "rejected", "detail": "directory push must be an object"}, status_code=400)
    try:
        result = await asyncio.to_thread(store_directory_push, body)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": "rejected", "detail": str(exc)}, status_code=400)
    return JSONResponse(result)


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
            log.warning("caller header rejected: %s", e)
            return JSONResponse(
                {"ok": False, "error": "forbidden", "detail": "caller id header is not allowed for this token"},
                status_code=403,
            )
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
