"""通話を結び付けた Codex の会話が、返信を受け取れる状態かを確かめる。

会話の記録（rollout）の末尾の読み方と、公式キューの見方は aiterm-steer-delivery（codex-wake.js）と同じ。
引き継ぎ先は Throughline の公開コマンド `throughline auto-handoff status --json` から読む。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

_TAIL_FIRST_BYTES = 256 * 1024
_TAIL_MAX_BYTES = 16 * 1024 * 1024
_TURN_EVENTS = {"task_started": "running", "task_complete": "completed", "turn_aborted": "interrupted"}
_HANDOFF_HOPS = 20
_THROUGHLINE_TIMEOUT = 20

Request = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def read_turn_tail(file: str | os.PathLike[str]) -> str:
    """最後の番の状態。running／completed／interrupted／none（番が無い）／unknown（読めない）。"""
    try:
        with open(file, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            length = _TAIL_FIRST_BYTES
            while True:
                length = min(size, length)
                start = size - length
                handle.seek(start)
                lines = handle.read(length).decode("utf-8", "replace").split("\n")
                if start > 0:
                    lines = lines[1:]  # 途中から読んだ時、先頭は行の切れ端
                for line in reversed(lines):
                    if not any(event in line for event in _TURN_EVENTS):
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or row.get("type") != "event_msg":
                        continue
                    payload = row.get("payload")
                    state = _TURN_EVENTS.get(payload.get("type")) if isinstance(payload, dict) else None
                    if state:
                        return state
                if start == 0:
                    return "none"
                if length >= _TAIL_MAX_BYTES:
                    return "unknown"
                length *= 4
    except OSError:
        return "unknown"


@dataclass(frozen=True)
class Conversation:
    """`thread/read` で見た会話。exists が偽なら、同じ Codex 環境にその会話は無い。"""

    thread_id: str
    exists: bool
    source: str | None = None
    cwd: str | None = None
    turn: str = "unknown"
    # 会話の記録が最後に書かれてからの秒数。読めない時は、長い間止まっている物として扱う。
    idle_seconds: float = float("inf")

    @property
    def app(self) -> bool:
        return self.source == "vscode"


async def read_conversation(request: Request, thread_id: str,
                            missing: Callable[[Exception], bool]) -> Conversation:
    """会話の有無・種類・フォルダ・最後の番を読む。missing は「会話が無い」の応答を見分ける。"""
    try:
        read = await request("thread/read", {"threadId": thread_id, "includeTurns": False})
    except Exception as exc:
        if missing(exc):
            return Conversation(thread_id, False)
        raise
    thread = read.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        return Conversation(thread_id, False)
    source, cwd, path = thread.get("source"), thread.get("cwd"), thread.get("path")
    known = isinstance(path, str) and bool(path)
    try:
        idle = max(0.0, time.time() - os.stat(path).st_mtime) if known else float("inf")
    except OSError:
        idle = float("inf")
    return Conversation(
        thread_id, True,
        source if isinstance(source, str) else None,
        cwd if isinstance(cwd, str) and cwd else None,
        read_turn_tail(path) if known else "unknown",
        idle,
    )


async def queued_entry(request: Request, thread_id: str, delivery_id: str) -> str | None:
    """この配送の文が公式キューに残っていれば、そのキュー上のID。無ければ None。"""
    cursor = None
    while True:
        page = await request("thread/queue/list", {"threadId": thread_id, "cursor": cursor, "limit": 100})
        rows = page.get("data")
        for entry in rows if isinstance(rows, list) else []:
            if isinstance(entry, dict) and entry.get("clientUserMessageId") == delivery_id:
                return str(entry.get("id"))
        cursor = page.get("nextCursor")
        if not isinstance(cursor, str):
            return None


async def withdraw(request: Request, thread_id: str, delivery_id: str) -> bool:
    """キューに残っている文を取り消す。取り消した後にもう一度見て、無くなっていれば真。

    真が返った文は、その会話ではもう番にならない。偽の時は、番になったか残っているかを決められない。
    """
    entry = await queued_entry(request, thread_id, delivery_id)
    if entry is None:
        return False
    result = await request("thread/queue/delete", {"threadId": thread_id, "queuedSubmissionId": entry})
    return result.get("deleted") is True and await queued_entry(request, thread_id, delivery_id) is None


def _throughline() -> str | None:
    return os.environ.get("CALL_BRIDGE_THROUGHLINE") or shutil.which("throughline")


async def _throughline_json(args: list[str]) -> Any:
    """Throughline の読み取りの命令を1回流して、JSON の答えを返す。無い・失敗・読めない時は None。"""
    binary = _throughline()
    if not binary:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            binary, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), _THROUGHLINE_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode:
        return None
    try:
        return json.loads(stdout.decode("utf-8", "replace"))
    except ValueError:
        return None


async def handoff_operations(host: str = "codex") -> list[dict[str, Any]]:
    """Throughline の引き継ぎの一覧（新しい物から20件まで）。Throughline が無い・読めない時は空。"""
    value = await _throughline_json(["auto-handoff", "status", "--json"] + (["--host", host] if host != "codex" else []))
    rows = value.get("operations") if isinstance(value, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


@dataclass(frozen=True)
class Handoff:
    """止められた Codex の会話の、引き継ぎの状態。"""

    # 残っている一番先の続きの会話。無ければ None。
    current: str | None = None
    # 止めた直後で、Throughline が続きをまだ作っている。
    in_flight: bool = False
    # 引き継ぎが途中で止まっている時の、引き継ぎ ID と理由。Throughline の担当が resume でやり直す。
    stopped: str | None = None


async def handoff_state(thread_id: str) -> Handoff:
    """会話の続きを Throughline に尋ねる。

    0.16.15 以降の `throughline auto-handoff successor --thread <id> --json` を使う（件数の上限が無く、
    消された続きは数えず、作っている途中も分かる）。無い版では、一覧（20件まで）からたどる。
    """
    value = await _throughline_json(["auto-handoff", "successor", "--thread", thread_id, "--json"])
    if isinstance(value, dict) and value.get("schema") == "throughline.codex_auto_handoff_successor.v1":
        current, pending = value.get("current_thread_id"), value.get("pending")
        following = current if isinstance(current, str) and current and current != thread_id else None
        if isinstance(pending, dict) and following is None:
            if pending.get("in_flight") is True:
                return Handoff(in_flight=True)
            return Handoff(stopped=f"handoff_id={pending.get('handoff_id')} error_code={pending.get('error_code')}")
        return Handoff(current=following)
    return Handoff(current=successor(thread_id, await handoff_operations()))


def successor(thread_id: str, operations: list[dict[str, Any]]) -> str | None:
    """引き継ぎが済んだ（continued）会話の、今の続き。続きが無ければ None。"""
    targets = {
        row["source_thread_id"]: row["target_thread_id"] for row in operations
        if row.get("state") == "continued" and isinstance(row.get("source_thread_id"), str)
        and isinstance(row.get("target_thread_id"), str)
    }
    current, seen = thread_id, {thread_id}
    for _ in range(_HANDOFF_HOPS):
        following = targets.get(current)
        if following is None or following in seen:
            break
        current = following
        seen.add(current)
    return current if current != thread_id else None
