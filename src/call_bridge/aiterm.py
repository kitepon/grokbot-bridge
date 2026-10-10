"""端末の Aiterm（MCP）を呼んで、担当フォルダに席を立てる・席へ文を送る。

BellTeam がコンテナの席を起こすのと同じ道具（agent_launch／pty_list／pty_send）を、同じ意味で使う。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .codex_delivery import DeliveryError, state_root

# call-bridge の名前 → Aiterm の harness 名
HARNESSES = {"codex": "codex-cli", "claude-code": "claude-code", "cursor": "cursor-cli", "grok": "grok-cli"}
# Aiterm が「打つ前に断った」時に必ず付ける文。これがあれば、その文は席へ入っていない。
_NOT_SENT = "文字列は送信していません"
# 席を立てる呼び出しは、CLI が入力を受け付けるまで待つ。
_DIRECT_TIMEOUT = 300
_DIRECT_LINE_LIMIT = 16 * 1024 * 1024


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


class _Result:
    """MCP の道具の結果のうち、ここで使う3つだけ。"""

    def __init__(self, value: dict[str, Any]):
        self.isError = value.get("isError") is True
        self.structuredContent = value.get("structuredContent")
        rows = value.get("content")
        self.content = [SimpleNamespace(type=row.get("type"), text=row.get("text", ""))
                        for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict)]


class _DirectSession:
    """Aiterm と、標準入出力の JSON-RPC（MCP）で直接話す。

    Windows で使う。mcp の stdio client は、起こした process を「閉じる時に子ごと止める」ジョブへ入れる。
    Aiterm が立てた席（psmux の端末）もその子なので、接続を閉じた時に席ごと止まっていた
    （2026-10-10 に fox で、立てた席が数秒で消えた）。ここでは、ジョブへ入れずに起こす。
    """

    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.sequence = 0

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.sequence += 1
        request_id = self.sequence
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                                             "params": params}).encode() + b"\n")
        await self.process.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), _DIRECT_TIMEOUT)
            if not line:
                raise AitermError("AITERM_TRANSPORT_CLOSED", "Aiterm との接続が終了しました", sent=None)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                # Aiterm からの問い合わせには対応していない、と答える。
                self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {
                    "code": -32601, "message": "not supported"}}).encode() + b"\n")
                await self.process.stdin.drain()
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise AitermError("AITERM_REQUEST_REJECTED", str(message["error"]), sent=None)
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _Result:
        try:
            return _Result(await self.request("tools/call", {"name": name, "arguments": arguments}))
        except asyncio.TimeoutError as exc:
            raise AitermError("AITERM_REQUEST_TIMEOUT", f"{name} が時間内に返りませんでした", sent=None) from exc


@asynccontextmanager
async def _connect_direct(command: list[str]) -> AsyncIterator[Aiterm]:
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    process = None
    # 受け取り係がタスクのジョブの中に居る時は、そこからも出す。出られない設定のジョブなら、そのまま起こす。
    for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=_DIRECT_LINE_LIMIT, creationflags=flags | extra)
            break
        except OSError as exc:
            failure = exc
    if process is None:
        raise AitermError("AITERM_UNAVAILABLE", f"aiterm-mcp を起動できません: {failure}", sent=False)
    session = _DirectSession(process)
    try:
        try:
            await session.request("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "call-bridge", "version": "1"}})
        except asyncio.TimeoutError as exc:
            raise AitermError("AITERM_UNAVAILABLE", "aiterm-mcp が応答しません", sent=False) from exc
        assert process.stdin is not None
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await process.stdin.drain()
        yield Aiterm(session)  # type: ignore[arg-type]
    finally:
        # 入力を閉じて、Aiterm が自分で終わるのを待つ。終わらない時も、止めるのはこの process だけ（席は残す）。
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.terminate()
            await process.wait()


@asynccontextmanager
async def connect() -> AsyncIterator[Aiterm]:
    command = aiterm_command()
    if os.name == "nt":
        async with _connect_direct(command) as terminal:
            yield terminal
        return
    parameters = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield Aiterm(session)
