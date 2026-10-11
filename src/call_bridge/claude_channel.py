"""通話の返信を、生きている Claude Code の会話へ渡す。

共通パッケージ aiterm-steer-delivery の channel を使う。Claude Code の公式の hook（asyncRewake）が、
番を終えて止まっている会話を起こして本文を渡す。作業中の会話には、その番へ入る。
パッケージの CLI は Codex だけなので、call-bridge が持つ Node の入口（steer/*.mjs）から呼ぶ。
入口は置き場の steer/ へ写して使う。hook の登録が指す場所を、入れ直しで動かさないため。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .codex_delivery import (DeliveryError, _bridge_config, _steer_result, current_steer_cli, hidden_child,
                             private_dir, state_root, steer_profile)

HOOK = "call-bridge-claude-hook.mjs"
CLI = "call-bridge-channel.mjs"
DISPATCH_TOOLS = ["call_open", "call_adopt"]
# 0.4.2 より前は、Claude Code が起動し直して同じ会話を再開した後、開いてあった channel の本文が届かない。
MIN_VERSION = (0, 4, 2)
_TIMEOUT = 60
_ACTIVE_SECONDS = 120
# 会話を見分けられない・hook が無い時の理由。通話は開けるので、今までどおり会話が自分で取りに来る形にする。
UNAVAILABLE = ("CLAUDE_CHANNEL_UNAVAILABLE", "CLAUDE_PARENT_HOOK_UNAVAILABLE", "CLAUDE_PARENT_ID_UNAVAILABLE",
               "CLAUDE_PARENT_SUBAGENT_UNSUPPORTED", "CLAUDE_PARENT_UNSUPPORTED", "CLAUDE_PARENT_PROCESS_UNAVAILABLE")


def steer_dir() -> Path:
    return state_root() / "steer"


def settings_file() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def _version(library: Path) -> tuple[int, ...] | None:
    try:
        package = json.loads((library.parent.parent / "package.json").read_text(encoding="utf-8"))
        return tuple(int(part) for part in str(package["version"]).split("-")[0].split(".")[:3])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def install(mcp_server: str) -> dict[str, str]:
    """Node の入口を置き場へ写し、パッケージの場所と識別情報を隣へ書く。"""
    config = _bridge_config()
    cli = current_steer_cli(config)
    library = cli.parent / "index.js"
    if cli.suffix.lower() not in (".js", ".mjs") or not library.is_file():
        raise DeliveryError("STEER_DELIVERY_UNAVAILABLE", f"aiterm-steer-delivery の dist/index.js を特定できません: {cli}")
    version = _version(library)
    if version is None or version < MIN_VERSION:
        raise DeliveryError("STEER_DELIVERY_OUTDATED",
                            "Claude Code への自動の配送には aiterm-steer-delivery 0.4.2 以降が要ります。"
                            "npm install -g aiterm-steer-delivery@latest を実行してください")
    # 版つきの実体（Homebrew の Cellar など）ではなく、PATH にある入口を控える。Node を上げても場所が変わらない。
    node = shutil.which("node") or config.get("steer_node")
    if not isinstance(node, str) or not Path(node).is_file():
        raise DeliveryError("STEER_DELIVERY_UNAVAILABLE", "Node の実行ファイルが見つかりません")
    target = private_dir(steer_dir())
    for source in sorted((Path(__file__).parent / "steer").glob("*.mjs")):
        shutil.copyfile(source, target / source.name)
    profile = {**steer_profile(mcp_server), "dispatch_tools": DISPATCH_TOOLS}
    profile["hooks"] = {**profile["hooks"], "claude": HOOK}
    temporary = target / "steer.json.tmp"
    temporary.write_text(json.dumps({"library": str(library), "node": os.path.abspath(node), "profile": profile},
                                    ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target / "steer.json")
    return {"library": str(library), "version": ".".join(map(str, version))}


def _command() -> list[str]:
    directory = steer_dir()
    try:
        node = json.loads((directory / "steer.json").read_text(encoding="utf-8"))["node"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DeliveryError("CLAUDE_CHANNEL_UNAVAILABLE",
                            "call-bridge-setup harness claude-code enable を実行してください") from exc
    if not isinstance(node, str) or not Path(node).is_file():
        node = shutil.which("node")  # 控えた場所から Node が無くなった（入れ直しなど）。今の PATH から探す
    if not node or not (directory / CLI).is_file():
        raise DeliveryError("CLAUDE_CHANNEL_UNAVAILABLE", "call-bridge-setup harness claude-code enable を実行してください")
    return [node, str(directory / CLI)]


async def run(args: list[str], *, text: str | None = None, sends: bool = False) -> dict[str, Any]:
    """Node の入口を1回流す。sends は、入ったかどうかが分からなくなりうる送信。"""
    command = _command()
    try:
        process = await asyncio.create_subprocess_exec(
            *command, *args,
            stdin=asyncio.subprocess.PIPE if text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **hidden_child())
    except OSError as exc:
        raise DeliveryError("CLAUDE_CHANNEL_UNAVAILABLE", "Claude Code への配送の入口を起動できません") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(text.encode() if text is not None else None), _TIMEOUT)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise DeliveryError("CLAUDE_CHANNEL_TIMEOUT", "Claude Code への配送の入口が時間内に終わりませんでした",
                            outcome_unknown=sends) from exc
    return _steer_result(stdout, stderr, sends)


def run_sync(args: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run([*_command(), *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError("CLAUDE_CHANNEL_UNAVAILABLE", "Claude Code への配送の入口を実行できません") from exc
    return _steer_result(result.stdout, result.stderr, False)


def hooks(action: str) -> dict[str, Any]:
    """Claude Code の利用者の設定にある、call-bridge の hook を登録・解除・確認する。ほかの hook は変えない。"""
    return run_sync(["setup", action, "--settings", str(settings_file())])


async def open_channel(client_name: str | None, meta: dict[str, Any]) -> dict[str, str]:
    """道具を呼んだ会話に channel を開く。会話は、hook が残した記録と要求の toolUseId で見分ける。"""
    opened = await run(["open", "--client", client_name or "", "--meta", json.dumps(meta)])
    if not isinstance(opened.get("channel_id"), str) or not isinstance(opened.get("session_id"), str):
        raise DeliveryError("CLAUDE_CHANNEL_UNAVAILABLE", "channel を開いた結果を読めません")
    return {"channel_id": opened["channel_id"], "session_id": opened["session_id"]}


async def send(channel_id: str, delivery_id: str, text: str) -> None:
    await run(["send", "--channel", channel_id, "--delivery", delivery_id], text=text, sends=True)


async def delivery_state(channel_id: str, delivery_id: str) -> tuple[str | None, bool]:
    """本文の状態と、会話が生きているか。

    queued＝まだ誰も取っていない、sending＝出している途中、emitted＝会話へ出した、unknown＝取った後に止まった。
    生きているかは queued の時だけ調べる（ほかの時は偽）。channel を開いた Claude Code の process が居るか、
    会話の記録がこの2分のうちに書かれていれば、生きている。
    """
    value = await run(["state", "--channel", channel_id, "--delivery", delivery_id])
    state, age = value.get("state"), value.get("transcript_age")
    alive = value.get("parent_alive") is True or (isinstance(age, (int, float)) and age < _ACTIVE_SECONDS)
    return (state if isinstance(state, str) else None), alive


async def withdraw(channel_id: str, delivery_id: str) -> bool:
    """まだ誰も取っていない本文を取り下げる。真なら、会話へは出ていない。"""
    return (await run(["withdraw", "--channel", channel_id, "--delivery", delivery_id])).get("withdrawn") is True


async def close(channel_id: str) -> None:
    await run(["close", "--channel", channel_id])
