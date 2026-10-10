"""端末の Aiterm（MCP）を呼んで、担当フォルダに席を立てる・席へ文を送る。

BellTeam がコンテナの席を起こすのと同じ道具（agent_launch／pty_list／pty_send）を、同じ意味で使う。
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .codex_delivery import DeliveryError, state_root

# call-bridge の名前 → Aiterm の harness 名
HARNESSES = {"codex": "codex-cli", "claude-code": "claude-code", "cursor": "cursor-cli", "grok": "grok-cli"}
# Aiterm が「打つ前に断った」時に必ず付ける文。これがあれば、その文は席へ入っていない。
_NOT_SENT = "文字列は送信していません"


class AitermError(DeliveryError):
    """sent が偽なら、文は席へ入っていない。真か None（不明）の時は送り直さない。"""

    def __init__(self, code: str, detail: str, *, sent: bool | None, launch: dict[str, Any] | None = None):
        super().__init__(code, detail, outcome_unknown=sent is not False)
        self.sent = sent
        self.launch = launch


def aiterm_command() -> list[str]:
    """npm のシム（Windows の .cmd／.ps1）を避け、同じ場所の dist/index.js を Node で直接起こす。"""
    config_file = state_root() / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
    binary = os.environ.get("CALL_BRIDGE_AITERM") or config.get("aiterm_mcp") or shutil.which("aiterm-mcp")
    if not binary:
        raise AitermError("AITERM_UNAVAILABLE", "aiterm-mcp が見つかりません。npm install -g aiterm-mcp を実行してください",
                          sent=False)
    path = Path(binary).resolve()
    if path.suffix.lower() not in (".js", ".mjs"):
        script = Path(binary).parent / "node_modules" / "aiterm-mcp" / "dist" / "index.js"
        if not script.is_file():
            return [str(path)]
        path = script.resolve()
    node = config.get("steer_node") or config.get("node_binary") or shutil.which("node")
    if not isinstance(node, str) or not Path(node).is_file():
        raise AitermError("AITERM_UNAVAILABLE", "aiterm-mcp の Node 実行ファイルが見つかりません", sent=False)
    return [node, str(path)]


def _text(result: Any) -> str:
    return " ".join(item.text for item in result.content if getattr(item, "type", None) == "text")


class Aiterm:
    """1回の用事ごとに開いて閉じる、Aiterm への短い接続。"""

    def __init__(self, session: ClientSession):
        self.session = session

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self.session.call_tool(name, arguments)

    async def sessions(self) -> dict[str, str | None]:
        """動いている Aiterm の席。名前 → harness（席でない端末は None）。"""
        result = await self._call("pty_list", {})
        rows = result.structuredContent.get("sessions") if isinstance(result.structuredContent, dict) else None
        if result.isError or not isinstance(rows, list):
            raise AitermError("AITERM_LIST_FAILED", _text(result) or "席の一覧を読めません", sent=False)
        return {row["session_id"]: row.get("harness") for row in rows
                if isinstance(row, dict) and isinstance(row.get("session_id"), str)}

    async def launch(self, harness: str, cwd: str, session_name: str, prompt: str) -> tuple[str, bool]:
        """担当フォルダに席を立てて、最初の文を渡す。席の名前と、最初の文が入ったかを返す。

        入っていない時（席は起きたが、最初の文は打たれなかった）は、呼び出し側が send で送る。
        """
        result = await self._call("agent_launch", {
            "harness": HARNESSES[harness], "cwd": cwd, "session_name": session_name,
            "prompt": prompt, "trust_project": True,
        })
        structured = result.structuredContent if isinstance(result.structuredContent, dict) else {}
        launch = structured if structured.get("schema") == "aiterm.agent-launch-result.v1" else None
        delivery = launch.get("initial_prompt") if launch else None
        prompt_state = delivery.get("status") if isinstance(delivery, dict) else None
        if result.isError:
            startup = launch.get("startup") if launch else None
            blocked = isinstance(startup, dict) and startup.get("status") == "blocked"
            # 起動の準備が終わらなかった席は、端末だけが残る。最初の文は入っていない。
            not_sent = blocked or prompt_state == "not_sent" or _NOT_SENT in _text(result)
            raise AitermError("AITERM_LAUNCH_FAILED", _text(result) or "席を起こせません",
                              sent=False if not_sent else None, launch=launch)
        session = structured.get("session_id")
        if not isinstance(session, str) or not session or prompt_state not in ("started", "submitted_unconfirmed", "not_sent"):
            raise AitermError("AITERM_LAUNCH_RECEIPT_INVALID", "起こした席の結果を確認できません", sent=None, launch=launch)
        return session, prompt_state != "not_sent"

    async def send(self, session_id: str, text: str) -> None:
        """動いている席へ文を送る。席の登録が無い端末へは打たない。"""
        result = await self._call("pty_send", {"session_id": session_id, "text": text, "require_agent": True})
        if result.isError:
            message = _text(result)
            raise AitermError("AITERM_SEND_FAILED", message or "席へ送れません",
                              sent=False if _NOT_SENT in message else None)

    async def close(self, session_id: str) -> None:
        await self._call("pty_close", {"session_id": session_id})


@asynccontextmanager
async def connect() -> AsyncIterator[Aiterm]:
    command = aiterm_command()
    parameters = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield Aiterm(session)
