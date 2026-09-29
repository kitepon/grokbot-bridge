"""通話の返信を Codex 親へ配送する。

継続型の親は共通パッケージ aiterm-steer-delivery の CLI が公式キューへ入れ、
作業中の turn への差し込みもそのパッケージの hook が行う。
短命な codex exec の親への配送（inject と UserPromptSubmit hook）は call-bridge 独自に残す。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psutil

STEER_PROFILE = "steer-profile.json"
_STEER_TIMEOUT = 60


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


def _same_pid_namespace(pid: int) -> bool:
    if sys.platform != "linux":
        return True
    try:
        return os.stat(f"/proc/{pid}/ns/pid").st_ino == os.stat("/proc/self/ns/pid").st_ino
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DeliveryError("CODEX_PROCESS_UNAVAILABLE", "Codex の PID 名前空間を確認できません") from exc


def codex_command() -> list[str]:
    """Launch a Node based Codex even when the MCP child has a minimal PATH."""
    binary = codex_binary()
    if Path(binary).suffix.lower() != ".js":
        return [binary]
    config_file = state_root() / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
    node = config.get("node_binary") or shutil.which("node")
    if not isinstance(node, str) or not Path(node).is_file():
        raise DeliveryError("CODEX_RUNTIME_UNAVAILABLE", "Codex の Node 実行ファイルが見つかりません")
    return [node, binary]


def codex_processes() -> list[dict[str, int | float]]:
    """Codex 本体の PID と生成時刻を保存し、PID 再利用と区別する。"""
    processes = []
    try:
        for process in psutil.process_iter(["name", "exe", "create_time"], ad_value=None):
            name = (process.info["name"] or "").lower()
            executable = Path(process.info["exe"] or "").name.lower()
            if name not in ("codex", "codex.exe") and executable not in ("codex", "codex.exe"):
                continue
            if not _same_pid_namespace(process.pid):
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
        process = psutil.Process(row["pid"])
        return _same_pid_namespace(process.pid) and _process_matches(process, [row])
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
    if (len(ours) != 1 or ours[0].get("eventName") != "userPromptSubmit" or
            ours[0].get("async") or ours[0].get("handlerType") != "command"):
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
                *codex_command(), "app-server", "--listen", "stdio://",
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
        outcome_unknown = method in ("thread/queue/add", "thread/inject_items")
        try:
            self.process.stdin.write(body)
            await self.process.stdin.drain()
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), self.timeout)
                if not line:
                    raise DeliveryError("CODEX_TRANSPORT_CLOSED", "Codex の接続が終了しました",
                                        outcome_unknown=outcome_unknown)
                response = json.loads(line)
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise DeliveryError("CODEX_REQUEST_REJECTED", str(response["error"]))
                result = response.get("result")
                if not isinstance(result, dict):
                    raise DeliveryError("CODEX_RESPONSE_INVALID", "Codex の応答を認識できません",
                                        outcome_unknown=outcome_unknown)
                return result
        except asyncio.TimeoutError as exc:
            raise DeliveryError("CODEX_REQUEST_TIMEOUT", f"{method} が時間内に返りませんでした",
                                outcome_unknown=outcome_unknown) from exc
        except (BrokenPipeError, ConnectionError, json.JSONDecodeError) as exc:
            raise DeliveryError("CODEX_TRANSPORT_FAILED", "Codex との通信が失敗しました",
                                outcome_unknown=outcome_unknown) from exc


def steer_profile(mcp_server: str) -> dict[str, Any]:
    """aiterm-steer-delivery へ渡す call-bridge の識別情報。置き場は state directory にまとめる。"""
    root = str(state_root().resolve())
    return {
        "id": "call-bridge", "display_name": "call-bridge",
        "setup_command": "call-bridge-setup enable", "codex_steer_command": "call-bridge-setup enable",
        "mcp_server": mcp_server, "dispatch_tools": ["call_open"],
        "state_root": root, "config_root": root,
        "hooks": {"codex": "call-bridge-codex-hook.js", "claude": "call-bridge-claude-hook.js",
                  "cursor": "call-bridge-cursor-hook.js"},
        "codex_client_name": "grokbot_bridge_parent_delivery",
        "codex_hook_schema": "call-bridge.codex-parent-hooks.v1",
        "backup_suffix": ".call-bridge-backup",
    }


def _steer_command(config: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    cli = os.environ.get("AITERM_STEER_DELIVERY") or config.get("steer_cli") or shutil.which("aiterm-steer-delivery")
    if not cli:
        raise DeliveryError("STEER_DELIVERY_UNAVAILABLE",
                            "aiterm-steer-delivery が見つかりません。npm install -g aiterm-steer-delivery を実行してください")
    env = dict(os.environ)
    command = [cli]
    node = config.get("steer_node")
    if Path(cli).suffix.lower() in (".js", ".mjs"):
        node = node or shutil.which("node")
        if not isinstance(node, str) or not Path(node).is_file():
            raise DeliveryError("STEER_DELIVERY_UNAVAILABLE", "aiterm-steer-delivery の Node 実行ファイルが見つかりません")
        command = [node, cli]
    # MCP の PATH が狭くても、パッケージが setup で記録した Codex と Node を使えるようにする。
    for runtime in (node, config.get("node_binary")):
        if isinstance(runtime, str):
            env["PATH"] = os.pathsep.join([str(Path(runtime).parent), env.get("PATH", "")])
    binary = os.environ.get("CODEX_CLI_PATH") or config.get("codex_binary")
    if isinstance(binary, str) and "CODEX_BIN" not in env:
        env["CODEX_BIN"] = binary
    return command, env


def _steer_arguments(args: list[str]) -> list[str]:
    profile = state_root() / STEER_PROFILE
    if not profile.is_file():
        raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "call-bridge-setup enable を実行してください")
    return ["--profile", str(profile), "codex", *args]


def _steer_result(stdout: bytes, stderr: bytes, outcome_unknown: bool) -> dict[str, Any]:
    lines = stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        value = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        detail = stderr.decode("utf-8", "replace").strip()[-500:] or "応答がありません"
        raise DeliveryError("STEER_DELIVERY_INVALID", f"aiterm-steer-delivery の応答を認識できません: {detail}",
                            outcome_unknown=outcome_unknown)
    if value["ok"]:
        return value
    code = value.get("code") if isinstance(value.get("code"), str) else "STEER_DELIVERY_FAILED"
    message = str(value.get("message", ""))
    raise DeliveryError(code, message.removeprefix(f"{code}: "), outcome_unknown=value.get("outcome_unknown") is True)


async def steer(args: list[str], *, text: str | None = None, sends: bool = False) -> dict[str, Any]:
    """aiterm-steer-delivery の codex 命令を一度だけ実行する。sends は受付の成否が不明になりうる送信。"""
    command, env = _steer_command(_bridge_config())
    try:
        process = await asyncio.create_subprocess_exec(
            *command, *_steer_arguments(args),
            stdin=asyncio.subprocess.PIPE if text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
    except OSError as exc:
        raise DeliveryError("STEER_DELIVERY_UNAVAILABLE", "aiterm-steer-delivery を起動できません") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(text.encode() if text is not None else None), _STEER_TIMEOUT)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise DeliveryError("STEER_DELIVERY_TIMEOUT", "aiterm-steer-delivery が時間内に終わりませんでした",
                            outcome_unknown=sends) from exc
    return _steer_result(stdout, stderr, sends)


def steer_sync(args: list[str], config: dict[str, Any]) -> dict[str, Any]:
    """setup から codex setup 命令を実行する。"""
    command, env = _steer_command(config)
    try:
        result = subprocess.run([*command, *_steer_arguments(args)], capture_output=True,
                                stdin=subprocess.DEVNULL, env=env, timeout=_STEER_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError("STEER_DELIVERY_UNAVAILABLE", "aiterm-steer-delivery を実行できません") from exc
    return _steer_result(result.stdout, result.stderr, False)


def _bridge_config() -> dict[str, Any]:
    config_file = state_root() / "config.json"
    return json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}


async def verify_parent(thread_id: str, home: Path) -> str | None:
    try:
        uuid.UUID(thread_id)
    except ValueError as exc:
        raise DeliveryError("CODEX_PARENT_ID_INVALID", "親タスクIDが不正です") from exc
    config = _bridge_config()
    if not config:
        raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "call-bridge-setup enable を実行してください")
    if config.get("enabled") is False:
        raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "自動配送が無効です")
    if config.get("codex_home") and Path(config["codex_home"]).resolve() != home:
        raise DeliveryError("CODEX_HOOK_HOME_MISMATCH", "配送先と hook の Codex 環境が一致しません")
    assert_parent_current(config)
    command = config.get("hook_command")
    if not isinstance(command, str):
        raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "配送 hook の設定がありません")
    # 親の確認と、Steer の hook が有効ならその登録の確認はパッケージが行う。
    await steer(["verify", "--thread", thread_id, "--codex-home", str(home)])
    async with CodexRPC(home) as rpc:
        result = await rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise DeliveryError("CODEX_PARENT_UNAVAILABLE", "同じ Codex 環境に親タスクがありません")
        source = thread.get("source")
        hooks = await rpc.request("hooks/list", {"cwds": [thread.get("cwd") or str(home)]})
        ours = owned_hooks(hooks, command, home)
        if any(not row.get("enabled") or row.get("trustStatus") not in ("trusted", "managed") for row in ours):
            raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "配送 hook が有効ではありません")
        return source if isinstance(source, str) else None


def _write_json_once(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.write("\n")


async def hook_delivery_state(thread_id: str, home: Path, delivery_id: str) -> str | None:
    """パッケージの hook が公式キューから取り出し中なら sending、中断したなら unknown。"""
    # Steer の hook を一度も使っていない端末では、Node を起動せずに状態なしとする。
    if not (state_root() / "codex-parent-hooks" / "inputs").is_dir():
        return None
    result = await steer(["state", "--thread", thread_id, "--delivery", delivery_id, "--codex-home", str(home)])
    state = result.get("state")
    if state not in (None, "sending", "unknown"):
        raise DeliveryError("CODEX_HOOK_STATE_INVALID", "hook の配送状態が不正です")
    return state


async def submit_reply(thread_id: str, home: Path, delivery_id: str, text: str) -> str:
    async with CodexRPC(home) as rpc:
        read = await rpc.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = read.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise DeliveryError("CODEX_PARENT_UNAVAILABLE", "同じ Codex 環境に親タスクがありません")
        if thread.get("source") == "exec":
            try:
                await rpc.request("thread/resume", {"threadId": thread_id})
            except DeliveryError as exc:
                if exc.code == "CODEX_REQUEST_REJECTED" and "already has an active writer" in str(exc):
                    return "deferred"
                raise
            marker = state_root() / "codex-inputs" / thread_id / "injections" / f"{delivery_id}.json"
            try:
                _write_json_once(marker, {"thread_id": thread_id, "delivery_id": delivery_id,
                                          "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
            except FileExistsError as exc:
                raise DeliveryError("DELIVERY_ALREADY_STARTED", "同じ返信の配送記録があります",
                                    outcome_unknown=True) from exc
            except OSError as exc:
                raise DeliveryError("DELIVERY_STATE_WRITE_FAILED", "配送記録を保存できません") from exc
            await rpc.request("thread/inject_items", {
                "threadId": thread_id,
                "items": [{"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": text}]}],
            })
            return "injected"
    result = await steer(["submit", "--thread", thread_id, "--delivery", delivery_id,
                          "--text-file", "-", "--codex-home", str(home)], text=text, sends=True)
    receipt = result.get("queued_submission_id")
    if not isinstance(receipt, str):
        raise DeliveryError("CODEX_QUEUE_RECEIPT_INVALID", "キュー受付IDを確認できません", outcome_unknown=True)
    return receipt


async def claim_exec_replies(event: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, int]], list[str]]:
    """Fetch exec replies when its parent starts a prompt; no child watcher must survive."""
    from . import local
    import httpx

    thread_id = event.get("session_id")
    if event.get("hook_event_name") != "UserPromptSubmit" or not isinstance(thread_id, str):
        raise DeliveryError("CODEX_HOOK_INPUT_INVALID", "hook の入力が不正です")
    try:
        if str(uuid.UUID(thread_id)) != thread_id:
            raise ValueError(thread_id)
    except ValueError as exc:
        raise DeliveryError("CODEX_HOOK_INPUT_INVALID", "親タスクIDが不正です") from exc
    store = local.LocalStore(state_root())
    subscriptions = [row for row in store.active_exec(thread_id)
                     if Path(row["codex_home"]).resolve() == codex_home()]
    if not subscriptions:
        return {}, [], []
    texts: list[str] = []
    reserved: list[tuple[str, int]] = []
    closed: list[str] = []
    async with httpx.AsyncClient(headers=local._headers(), timeout=5) as client:
        for row in subscriptions:
            session_id = row["session_id"]
            try:
                response = await client.get(local._rest_url(session_id), params={
                    "party": "local", "after_seq": row["after_seq"],
                })
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict) or value.get("ok") is not True or not isinstance(value.get("messages"), list):
                    raise DeliveryError("BRIDGE_POLL_INVALID", "通話の受信応答が不正です")
                store.transport_error(session_id, None)
            except (httpx.HTTPError, ValueError) as exc:
                store.transport_error(session_id, f"BRIDGE_POLL_FAILED: {exc}")
                continue
            last_seq = row["after_seq"]
            for message in value["messages"]:
                if not isinstance(message, dict):
                    store.stop(session_id, "failed", "BRIDGE_MESSAGE_INVALID")
                    break
                seq, body = message.get("seq"), message.get("message")
                if type(seq) is not int or seq <= last_seq or not isinstance(body, str):
                    store.stop(session_id, "failed", "BRIDGE_MESSAGE_INVALID")
                    break
                _delivery_id, state = store.reserve(session_id, seq)
                if state in ("submitted", "injected"):
                    store.submitted(session_id, seq, state)
                elif state in ("new", "waiting"):
                    texts.append(local.reply_text(row, seq, body))
                    reserved.append((session_id, seq))
                else:
                    store.stop(session_id, "unknown", "DELIVERY_PREVIOUSLY_STARTED", seq)
                    break
                last_seq = seq
            else:
                if value.get("status") == "hungup":
                    closed.append(session_id)
    if not texts:
        return {}, reserved, closed
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": "\n\n".join(texts)}}, reserved, closed


def hook_main() -> None:
    """codex exec の親へ、次のプロンプトで返信を渡す UserPromptSubmit hook。"""
    exec_reserved: list[tuple[str, int]] = []
    try:
        event = json.load(sys.stdin)
        if event.get("hook_event_name") == "UserPromptSubmit":
            output, exec_reserved, closed = asyncio.run(claim_exec_replies(event))
        else:
            # 旧版の PostToolUse／Stop hook を読み込んだまま再起動前の Codex からも呼ばれる。
            # 何も取り出さず、キューの返信は公式キューが次のターンで届ける。
            output, closed = {}, []
        sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if exec_reserved or closed:
            from . import local
            store = local.LocalStore(state_root())
            for session_id, seq in exec_reserved:
                store.submitted(session_id, seq, "injected")
            for session_id in closed:
                store.stop(session_id, "closed")
    except Exception as exc:
        if exec_reserved:
            from . import local
            store = local.LocalStore(state_root())
            for session_id, seq in exec_reserved:
                store.stop(session_id, "unknown", "CODEX_HOOK_DELIVERY_UNCONFIRMED", seq)
        print(f"CALL_BRIDGE_HOOK_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    hook_main()
