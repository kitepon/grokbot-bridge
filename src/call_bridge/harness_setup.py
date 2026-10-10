"""Claude Code・Cursor・Grok へ、ローカル MCP（call_bridge.local）を登録する。

Codex の登録は setup.py が公式の設定 API で行う。ここは残りの3つで、各 CLI の公式の登録先だけを書く。
登録するのは call-bridge の1項目だけで、ほかの MCP とほかの設定は変えない。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .codex_delivery import DeliveryError, state_root

NAME = "call-bridge"
HARNESSES = ("claude-code", "cursor", "grok")
_CLI = {"claude-code": "claude", "grok": "grok"}
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _local_command() -> list[str]:
    return [sys.executable, "-m", "call_bridge.local"]


def _run(command: list[str]) -> "subprocess.CompletedProcess[str]":
    # 利用者の設定だけを見る場所で動かす。プロジェクトのフォルダで動かすと、そのフォルダだけの登録が先に見える。
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                              cwd=state_root())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError("HARNESS_SETUP_FAILED", f"{command[0]} を実行できません: {exc}") from exc


def _cli(harness: str) -> str:
    binary = shutil.which(_CLI[harness])
    if not binary:
        raise DeliveryError("HARNESS_UNAVAILABLE", f"{_CLI[harness]} が見つかりません")
    return binary


def cursor_file() -> Path:
    return Path(os.environ.get("CURSOR_HOME") or Path.home() / ".cursor") / "mcp.json"


def _cursor_read() -> dict[str, Any]:
    file = cursor_file()
    if not file.is_file():
        return {}
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DeliveryError("HARNESS_CONFIG_INVALID", f"{file} を読めません。直してからもう一度実行してください") from exc
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers", {}), dict):
        raise DeliveryError("HARNESS_CONFIG_INVALID", f"{file} の形を認識できません")
    return value


def _cursor_write(value: dict[str, Any]) -> None:
    file = cursor_file()
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + ".call-bridge.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, file)


def require_token() -> None:
    """ローカル MCP は、この端末の auth.json の合言葉で通話のサーバーへつなぐ。先に用意があることを確かめる。"""
    config_file = state_root() / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.is_file() else {}
    token_env = config.get("token_env", "CALL_BRIDGE_TOKEN")
    if config.get("enabled") is True and (state_root() / "auth.json").is_file():
        return
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise DeliveryError("BRIDGE_TOKEN_MISSING",
                            f"{token_env} がありません。Codex で call-bridge-setup enable を済ませるか、この変数を渡してください")
    from .setup import _write_json
    _write_json(state_root() / "auth.json", {"token": token})
    _write_json(config_file, {**config, "enabled": True, "token_env": token_env,
                              "mcp_url": config.get("mcp_url") or os.environ.get("CALL_BRIDGE_MCP_URL")
                              or "https://call.kitepon.dev/mcp"})


def enable(harness: str, run: Runner = _run) -> dict[str, str]:
    if harness not in HARNESSES:
        raise DeliveryError("HARNESS_UNSUPPORTED", f"{harness} には対応していません")
    require_token()
    command = _local_command()
    if harness == "cursor":
        value = _cursor_read()
        servers = dict(value.get("mcpServers", {}))
        servers[NAME] = {"command": command[0], "args": command[1:]}
        _cursor_write({**value, "mcpServers": servers})
    else:
        cli = _cli(harness)
        # 同じ名前の登録（HTTP 直結など）を、ローカル MCP へ置き換える。無い時の remove の失敗は数えない。
        run([cli, "mcp", "remove", "--scope", "user", NAME])
        added = run([cli, "mcp", "add", "--scope", "user", NAME, "--", *command] if harness == "claude-code"
                    else [cli, "mcp", "add", "--scope", "user", NAME, command[0], "--", *command[1:]])
        if added.returncode:
            raise DeliveryError("HARNESS_SETUP_FAILED", (added.stderr or added.stdout).strip()[-300:])
    return status(harness, run)


def disable(harness: str, run: Runner = _run) -> dict[str, str]:
    if harness not in HARNESSES:
        raise DeliveryError("HARNESS_UNSUPPORTED", f"{harness} には対応していません")
    if harness == "cursor":
        value = _cursor_read()
        servers = dict(value.get("mcpServers", {}))
        if NAME in servers:
            del servers[NAME]
            _cursor_write({**value, "mcpServers": servers})
    else:
        run([_cli(harness), "mcp", "remove", "--scope", "user", NAME])
    return status(harness, run)


def status(harness: str, run: Runner = _run) -> dict[str, str]:
    """registered＝ローカル MCP として登録がある。restart は、動いている会話には次の起動から効く、の意味。"""
    if harness not in HARNESSES:
        raise DeliveryError("HARNESS_UNSUPPORTED", f"{harness} には対応していません")
    if harness == "cursor":
        entry = _cursor_read().get("mcpServers", {}).get(NAME)
        local = isinstance(entry, dict) and "call_bridge.local" in json.dumps(entry.get("args", []))
    else:
        binary = shutil.which(_CLI[harness])
        if not binary:
            return {"harness": harness, "status": "unavailable"}
        shown = run([binary, "mcp", "get", NAME] if harness == "claude-code" else [binary, "mcp", "list"])
        local = shown.returncode == 0 and "call_bridge.local" in (shown.stdout + shown.stderr)
    return {"harness": harness, "status": "registered" if local else "not_registered"}
