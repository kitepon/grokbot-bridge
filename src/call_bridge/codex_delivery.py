"""GrokBot の返信を Codex のローカルキューへ配送する。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import psutil


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


def codex_processes() -> list[dict[str, int | float]]:
    """Codex 本体の PID と生成時刻を保存し、PID 再利用と区別する。"""
    processes = []
    try:
        for process in psutil.process_iter(["name", "exe", "create_time"], ad_value=None):
            name = (process.info["name"] or "").lower()
            executable = Path(process.info["exe"] or "").name.lower()
            if name not in ("codex", "codex.exe") and executable not in ("codex", "codex.exe"):
                continue
            created = process.info["create_time"]
            if not isinstance(created, (int, float)):
                raise DeliveryError("CODEX_PROCESS_UNAVAILABLE", "Codex の生成時刻を確認できません")
            processes.append({"pid": process.pid, "create_time": created})
    except psutil.Error as exc:
        raise DeliveryError("CODEX_PROCESS_UNAVAILABLE", "Codex のプロセスを確認できません") from exc
    return processes


def _stale_processes(config: dict[str, Any]) -> list[dict[str, int | float]]:
    rows = config.get("stale_processes", [])
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or type(row.get("pid")) is not int or
        not isinstance(row.get("create_time"), (int, float)) for row in rows
    ):
        raise DeliveryError("CODEX_PROCESS_STATE_INVALID", "Codex の再起動記録が不正です")
    return rows


def _process_matches(process: psutil.Process, stale: list[dict[str, int | float]]) -> bool:
    return any(row["pid"] == process.pid and row["create_time"] == process.create_time()
               for row in stale if row["pid"] == process.pid)


def _process_alive(row: dict[str, int | float]) -> bool:
    try:
        return _process_matches(psutil.Process(row["pid"]), [row])
    except psutil.NoSuchProcess:
        return False
    except psutil.Error as exc:
        raise DeliveryError("CODEX_PROCESS_UNAVAILABLE", "プロセスの生成時刻を確認できません") from exc


def restart_required(config: dict[str, Any]) -> bool:
    return any(_process_alive(row) for row in _stale_processes(config))


def assert_parent_current(config: dict[str, Any]) -> None:
    stale = _stale_processes(config)
    if not stale:
        return
    try:
        process = psutil.Process()
        if any(_process_matches(parent, stale) for parent in [process, *process.parents()]):
            raise DeliveryError("CODEX_STEER_RESTART_REQUIRED", "親 Codex を完全終了して再起動してください")
    except psutil.Error as exc:
        raise DeliveryError("CODEX_PROCESS_UNAVAILABLE", "親 Codex のプロセスを確認できません") from exc


def owned_hooks(response: dict[str, Any], command: str, home: Path) -> list[dict[str, Any]]:
    data = response.get("data")
    if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict) or
            data[0].get("errors") or not isinstance(data[0].get("hooks"), list)):
        raise DeliveryError("CODEX_HOOK_LIST_INVALID", "Codex の hook 一覧を認識できません")
    source = str((home / "hooks.json").resolve())
    rows = data[0]["hooks"]
    ours = [row for row in rows if isinstance(row, dict) and row.get("command") == command and
            row.get("sourcePath") == source]
    if (len(ours) != 2 or {row.get("eventName") for row in ours} != {"postToolUse", "stop"} or
            any(row.get("async") or row.get("handlerType") != "command" for row in ours)):
        raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "登録した同期 hook が Codex から見えません")
    return ours


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
        if config.get("codex_home") and Path(config["codex_home"]).resolve() != home:
            raise DeliveryError("CODEX_HOOK_HOME_MISMATCH", "配送先と hook の Codex 環境が一致しません")
        assert_parent_current(config)
        command = config.get("hook_command")
        if not isinstance(command, str):
            raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "配送 hook の設定がありません")
        hooks = await rpc.request("hooks/list", {"cwds": [thread.get("cwd") or str(home)]})
        ours = owned_hooks(hooks, command, home)
        if any(not row.get("enabled") or row.get("trustStatus") not in ("trusted", "managed") for row in ours):
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


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _set_claim_state(path: Path, state: str) -> None:
    _write_json(path, {**json.loads(path.read_text(encoding="utf-8")), "state": state})


def hook_delivery_state(thread_id: str, delivery_id: str) -> str | None:
    claim = state_root() / "codex-inputs" / thread_id / "claims" / f"{delivery_id}.json"
    try:
        value = json.loads(claim.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DeliveryError("CODEX_HOOK_STATE_INVALID", "hook の配送記録を読めません") from exc
    state = value.get("state")
    if state == "unknown":
        return "unknown"
    if state == "deleting":
        if type(value.get("pid")) is not int or not isinstance(value.get("create_time"), (int, float)):
            raise DeliveryError("CODEX_HOOK_STATE_INVALID", "hook のプロセス記録が不正です")
        return "sending" if _process_alive(value) else "unknown"
    if state in ("emitted", "not_in_queue") or state is None:
        return None
    raise DeliveryError("CODEX_HOOK_STATE_INVALID", "hook の配送状態が不正です")


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
    try:
        async with CodexRPC(home) as rpc:
            result = await rpc.request("thread/queue/add", {
                "threadId": thread_id,
                "clientUserMessageId": delivery_id,
                "input": [{"type": "text", "text": text, "text_elements": []}],
            })
    finally:
        settled = pending.parent.parent / "settled" / pending.name
        _write_json(settled, {"delivery_id": delivery_id})
        if not pending.exists():
            settled.unlink(missing_ok=True)
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
    config_file = state_root() / "config.json"
    if config_file.exists():
        config = json.loads(config_file.read_text(encoding="utf-8"))
        if config.get("codex_home") and Path(config["codex_home"]).resolve() != codex_home():
            raise DeliveryError("CODEX_HOOK_HOME_MISMATCH", "配送先と hook の Codex 環境が一致しません")
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
            entries = await _queued(rpc, thread_id)
            queued_ids = {item.get("clientUserMessageId") for item in entries}
            settled_dir = pending_dir.parent / "settled"
            for source in pending_dir.glob("*.json"):
                settled = settled_dir / source.name
                if source.stem not in queued_ids and settled.is_file():
                    source.unlink(missing_ok=True)
                    settled.unlink(missing_ok=True)
            for item in entries:
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
                settled = settled_dir / source.name
                settled.unlink(missing_ok=True)
                identity = psutil.Process()
                _write_json(claim, {**owner, "text": text, "state": "deleting",
                                    "turn_id": turn_id, "queued_submission_id": item.get("id"),
                                    "pid": identity.pid, "create_time": identity.create_time()})
                result = await rpc.request("thread/queue/delete", {
                    "threadId": thread_id, "queuedSubmissionId": item.get("id"),
                })
                if result.get("deleted") is True:
                    texts.append(text)
                else:
                    _set_claim_state(claim, "not_in_queue")
                    claimed.pop()
    except Exception:
        for claim in claimed:
            _set_claim_state(claim, "unknown")
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
            _set_claim_state(path, "emitted")
    except Exception as exc:
        for path in claimed:
            _set_claim_state(path, "unknown")
        print(f"CALL_BRIDGE_HOOK_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    hook_main()
