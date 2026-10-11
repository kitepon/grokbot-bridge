"""Claude Code に止められた裏の会話（claude --bg）を、同じ会話のまま起こす。

Claude Code は、裏の会話が「済んだ」と申告して動かないまま約1時間たつと、その process を止める（会話の記録は残す）。
Throughline が立てる後継は裏の会話なので、相手の返信を待っている役の会話が、これで止まる
（2026-10-11 に fox で、Claude Code の log に `bg retire e50377e1: settled, idle 61m`）。
止められた会話は、Claude Code の公式の口 `claude --bg --resume <session id> <文>` で、同じ番号・同じ記憶・
保存された起動の設定のまま起きる。動いている会話に同じ命令を出すと写しが出来るので、先に一覧で確かめる。

会話の一覧は `claude agents --json --all`（読むだけ）。裏の会話の行は kind=background で、動いている時だけ pid が付く。
同じ会話をアプリや端末で開いている時は kind=interactive の行が別にあり、そこに pid が付く。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_delivery import DeliveryError, hidden_child

_TIMEOUT = 60
_SESSION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_STARTED = re.compile(r"backgrounded · ([0-9a-f]{8})")


def _claude() -> str | None:
    return os.environ.get("CALL_BRIDGE_CLAUDE") or shutil.which("claude")


async def _run(args: list[str], cwd: str | None = None, merge: bool = False) -> tuple[int, str] | None:
    """claude の命令を1回流して、終わりの番号と出力を返す。無い・起こせない・終わらない時は None。

    出力は file で受ける。`claude --bg` は裏方の process を起こす事があり、pipe で受けると、
    その process が口を持ったままの時に読み終わらない。
    """
    binary = _claude()
    if not binary:
        return None
    with tempfile.TemporaryFile() as output:
        try:
            process = await asyncio.create_subprocess_exec(
                binary, *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=output,
                stderr=asyncio.subprocess.STDOUT if merge else asyncio.subprocess.DEVNULL, **hidden_child())
        except OSError:
            return None
        try:
            code = await asyncio.wait_for(process.wait(), _TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return None
        output.seek(0)
        return code, output.read().decode("utf-8", "replace")


async def sessions() -> list[dict[str, Any]] | None:
    """Claude Code が知っている会話の一覧（終わった裏の会話も含む）。読めない時は None。"""
    result = await _run(["agents", "--json", "--all"])
    if result is None or result[0] != 0:
        return None
    try:
        rows = json.loads(result[1])
    except ValueError:
        return None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None


@dataclass(frozen=True)
class Background:
    """Claude Code の裏の会話。running は、その会話の process がどこかで動いているか（裏でも、アプリでも）。"""

    session_id: str
    cwd: str
    running: bool


async def background(session_id: str) -> Background | None:
    """その会話が裏の会話なら、フォルダと、動いているか。裏の会話でない・一覧を読めない時は None。"""
    rows = await sessions()
    if rows is None:
        return None
    mine = [row for row in rows if row.get("sessionId") == session_id]
    job = next((row for row in mine if row.get("kind") == "background"), None)
    if job is None or not isinstance(job.get("cwd"), str) or not job["cwd"]:
        return None
    return Background(session_id, job["cwd"], any(isinstance(row.get("pid"), int) for row in mine))


def wake_prompt(calls: list[str]) -> str:
    """起こす時に渡す文。決まった文と通話の番号だけの1行。

    返信の本文と相手の名前は入れない。命令の引数は、同じ端末のほかの process から見える。
    Windows では claude が .cmd の入口を通るので、改行と、cmd が読み替える字（% & | < > ^ "）も入れない。
    返信は、起きた会話が call_adopt を呼んだ後に、いつもの受け口から渡る。
    """
    ids = "、".join(f"session_id={call}" for call in calls)
    return ("通話MCP（call-bridge）からの知らせです。この会話は止まっていたので、届いた通話の返信を渡すために起こしました。"
            "作業の前に、call-bridge の call_adopt を次の通話ごとに1回ずつ呼んでください（引数は session_id だけ）: "
            f"{ids} 。呼ぶと、届いている返信がこの会話へ渡り、この後の返信も自動で届きます。")


async def wake(found: Background, calls: list[str]) -> str:
    """止まっている裏の会話を起こす。起きた会話の番号の頭8字を返す（同じ会話なら、会話の番号の頭と同じ）。

    DeliveryError は、起こせなかった時。返信の本文は渡していないので、席へ渡す道はそのまま使える。
    """
    if not _SESSION.match(found.session_id) or not calls or not all(_SESSION.match(call) for call in calls):
        raise DeliveryError("CLAUDE_BG_ARGUMENT", "会話か通話の番号の形が違います")
    if not Path(found.cwd).is_dir():
        raise DeliveryError("CLAUDE_BG_FOLDER_MISSING", "止まった会話のフォルダがありません")
    result = await _run(["--bg", "--resume", found.session_id, wake_prompt(calls)], cwd=found.cwd, merge=True)
    if result is None:
        raise DeliveryError("CLAUDE_BG_UNAVAILABLE", "claude の命令を流せませんでした")
    code, output = result
    started = _STARTED.search(output)
    if code != 0 or not started:
        detail = " ".join(output.split())[:200]
        raise DeliveryError("CLAUDE_BG_WAKE_FAILED", f"会話を起こせませんでした（exit {code}）: {detail}")
    return started.group(1)
