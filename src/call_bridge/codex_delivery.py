"""GrokBot の返信を Codex のローカルキューへ配送する。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any


class DeliveryError(RuntimeError):
    def __init__(self, code: str, detail: str, *, outcome_unknown: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.outcome_unknown = outcome_unknown


def state_root() -> Path:
    root = Path(os.environ.get("CALL_BRIDGE_STATE", "~/.grokbot-bridge")).expanduser()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()


def codex_binary() -> str:
    config_file = state_root() / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
    binary = os.environ.get("CODEX_CLI_PATH") or config.get("codex_binary") or shutil.which("codex")
    if not binary:
        raise DeliveryError("CODEX_UNAVAILABLE", "Codex CLI が見つかりません")
    return binary


class CodexRPC:
    """公式 App Server への短命な接続。"""

    def __init__(self, home: Path, timeout: float = 15):
        self.home = home
        self.timeout = timeout
        self.process: asyncio.subprocess.Process | None = None
        self.sequence = 0

    async def __aenter__(self) -> CodexRPC:
        try:
            self.process = await asyncio.create_subprocess_exec(
                codex_binary(), "app-server", "--listen", "stdio://",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "CODEX_HOME": str(self.home)},
            )
        except OSError as exc:
            raise DeliveryError("CODEX_UNAVAILABLE", "Codex App Server を起動できません") from exc
        await self.request("initialize", {
            "clientInfo": {"name": "grokbot_bridge_parent_delivery", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        assert self.process.stdin is not None
        self.process.stdin.write(b'{"method":"initialized"}\n')
        await self.process.stdin.drain()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self.process is not None
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.process is not None
        assert self.process.stdin is not None and self.process.stdout is not None
        self.sequence += 1
        request_id = self.sequence
        body = json.dumps({"id": request_id, "method": method, "params": params}).encode() + b"\n"
        try:
            self.process.stdin.write(body)
            await self.process.stdin.drain()
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), self.timeout)
                if not line:
                    raise DeliveryError("CODEX_TRANSPORT_CLOSED", "Codex の接続が終了しました",
                                        outcome_unknown=method == "thread/queue/add")
                response = json.loads(line)
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise DeliveryError("CODEX_REQUEST_REJECTED", str(response["error"]))
                result = response.get("result")
                if not isinstance(result, dict):
                    raise DeliveryError("CODEX_RESPONSE_INVALID", "Codex の応答を認識できません",
                                        outcome_unknown=method == "thread/queue/add")
                return result
        except asyncio.TimeoutError as exc:
            raise DeliveryError("CODEX_REQUEST_TIMEOUT", f"{method} が時間内に返りませんでした",
                                outcome_unknown=method == "thread/queue/add") from exc
        except (BrokenPipeError, ConnectionError, json.JSONDecodeError) as exc:
            raise DeliveryError("CODEX_TRANSPORT_FAILED", "Codex との通信が失敗しました",
                                outcome_unknown=method == "thread/queue/add") from exc


async def verify_parent(thread_id: str, home: Path) -> None:
    try:
        uuid.UUID(thread_id)
    except ValueError as exc:
        raise DeliveryError("CODEX_PARENT_ID_INVALID", "親タスクIDが不正です") from exc
    async with CodexRPC(home) as rpc:
        result = await rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise DeliveryError("CODEX_PARENT_UNAVAILABLE", "同じ Codex 環境に親タスクがありません")
        source = thread.get("source")
        if isinstance(source, dict) and isinstance(source.get("subAgent"), dict) and "thread_spawn" in source["subAgent"]:
            raise DeliveryError("CODEX_PARENT_UNSUPPORTED", "native sub-agent への配送は未対応です")
        await rpc.request("thread/queue/list", {"threadId": thread_id, "limit": 1})
        config_file = state_root() / "config.json"
        if not config_file.exists():
            raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "call-bridge-setup enable を実行してください")
        config = json.loads(config_file.read_text(encoding="utf-8"))
        if config.get("enabled") is False:
            raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "自動配送が無効です")
        command = config.get("hook_command")
        if not isinstance(command, str):
            raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "配送 hook の設定がありません")
        hooks = await rpc.request("hooks/list", {"cwds": [thread.get("cwd") or str(home)]})
        data = hooks.get("data")
        rows = data[0].get("hooks") if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
        ours = [row for row in rows if isinstance(row, dict) and row.get("command") == command] if isinstance(rows, list) else []
        if len(ours) != 2 or any(not row.get("enabled") or row.get("trustStatus") not in ("trusted", "managed") for row in ours):
            raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "配送 hook が有効ではありません")


def _pending_dir(thread_id: str) -> Path:
    directory = state_root() / "codex-inputs" / thread_id / "pending"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _write_json_once(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.write("\n")


async def submit_reply(thread_id: str, home: Path, delivery_id: str, text: str) -> str:
    pending = _pending_dir(thread_id) / f"{delivery_id}.json"
    try:
        _write_json_once(pending, {
            "thread_id": thread_id,
            "delivery_id": delivery_id,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        })
    except FileExistsError as exc:
        raise DeliveryError("DELIVERY_ALREADY_STARTED", "同じ返信の配送記録があります",
                            outcome_unknown=True) from exc
    except OSError as exc:
        raise DeliveryError("DELIVERY_STATE_WRITE_FAILED", "配送記録を保存できません") from exc
    async with CodexRPC(home) as rpc:
        result = await rpc.request("thread/queue/add", {
            "threadId": thread_id,
            "clientUserMessageId": delivery_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
        })
    item = result.get("queuedSubmission")
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise DeliveryError("CODEX_QUEUE_RECEIPT_INVALID", "キュー受付IDを確認できません", outcome_unknown=True)
    return item["id"]


async def _queued(rpc: CodexRPC, thread_id: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        result = await rpc.request("thread/queue/list", {
            "threadId": thread_id, "cursor": cursor, "limit": 100,
        })
        page = result.get("data")
        if not isinstance(page, list):
            raise DeliveryError("CODEX_QUEUE_INVALID", "キュー一覧を認識できません")
        entries.extend(item for item in page if isinstance(item, dict))
        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            return entries
        if not isinstance(next_cursor, str) or next_cursor == cursor:
            raise DeliveryError("CODEX_QUEUE_INVALID", "キューの次ページが不正です")
        cursor = next_cursor


async def claim_hook_replies(event: dict[str, Any]) -> tuple[dict[str, Any], list[Path]]:
    thread_id = event.get("session_id")
    turn_id = event.get("turn_id")
    kind = event.get("hook_event_name")
    if not isinstance(thread_id, str) or not isinstance(turn_id, str) or kind not in ("PostToolUse", "Stop"):
        raise DeliveryError("CODEX_HOOK_INPUT_INVALID", "hook の入力が不正です")
    try:
        if str(uuid.UUID(thread_id)) != thread_id:
            raise ValueError(thread_id)
    except ValueError as exc:
        raise DeliveryError("CODEX_HOOK_INPUT_INVALID", "親タスクIDが不正です") from exc
    pending_dir = _pending_dir(thread_id)
    if not any(pending_dir.iterdir()):
        return {}, []
    claims_dir = pending_dir.parent / "claims"
    claims_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    claimed: list[Path] = []
    texts: list[str] = []
    try:
        async with CodexRPC(codex_home(), timeout=5) as rpc:
            for item in await _queued(rpc, thread_id):
                delivery_id = item.get("clientUserMessageId")
                if not isinstance(delivery_id, str):
                    continue
                try:
                    if str(uuid.UUID(delivery_id)) != delivery_id:
                        continue
                except ValueError:
                    continue
                source = pending_dir / f"{delivery_id}.json"
                if not source.is_file():
                    continue
                inputs = item.get("input")
                if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict):
                    raise DeliveryError("CODEX_HOOK_INPUT_CHANGED", "キュー本文が変更されました")
                text = inputs[0].get("text")
                if inputs[0].get("type") != "text" or not isinstance(text, str):
                    raise DeliveryError("CODEX_HOOK_INPUT_CHANGED", "キュー本文が変更されました")
                owner = json.loads(source.read_text(encoding="utf-8"))
                if (owner.get("thread_id") != thread_id or owner.get("delivery_id") != delivery_id
                        or owner.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest()):
                    raise DeliveryError("CODEX_HOOK_INPUT_CHANGED", "配送記録とキュー本文が一致しません")
                claim = claims_dir / f"{delivery_id}.json"
                try:
                    os.link(source, claim)
                    source.unlink()
                except FileExistsError:
                    continue
                except FileNotFoundError:
                    continue
                claimed.append(claim)
                result = await rpc.request("thread/queue/delete", {
                    "threadId": thread_id, "queuedSubmissionId": item.get("id"),
                })
                if result.get("deleted") is True:
                    texts.append(text)
                else:
                    claim.write_text(json.dumps({**owner, "state": "not_in_queue"}), encoding="utf-8")
                    claimed.pop()
    except Exception:
        for claim in claimed:
            claim.write_text(json.dumps({"state": "unknown"}), encoding="utf-8")
        raise
    if not texts:
        return {}, claimed
    joined = "\n\n".join(texts)
    output = ({"decision": "block", "reason": joined} if kind == "Stop" else
              {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": joined}})
    return output, claimed


def hook_main() -> None:
    claimed: list[Path] = []
    try:
        event = json.load(sys.stdin)
        output, claimed = asyncio.run(claim_hook_replies(event))
        sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        for path in claimed:
            path.write_text(json.dumps({"state": "emitted"}), encoding="utf-8")
    except Exception as exc:
        for path in claimed:
            path.write_text(json.dumps({"state": "unknown"}), encoding="utf-8")
        print(f"CALL_BRIDGE_HOOK_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    hook_main()
