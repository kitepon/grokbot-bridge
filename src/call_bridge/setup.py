"""ローカル Codex 受信口と公式 hook を登録する。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .codex_delivery import CodexRPC, DeliveryError, codex_binary, codex_home, state_root

_NAMES = ("call-bridge", "grokbot-bridge")


def _command() -> str:
    if os.name == "nt":
        python = sys.executable.replace("'", "''")
        script = f"& '{python}' -m call_bridge.codex_delivery; exit $LASTEXITCODE"
        import base64
        return "pwsh.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16le")).decode()
    return f"{shlex.quote(sys.executable)} -m call_bridge.codex_delivery"


def _write_json(file: Path, value: dict[str, Any]) -> None:
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=file.name + ".", dir=file.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _backup_codex_config(home: Path) -> Path:
    backup = state_root() / f"codex-config-{uuid.uuid4()}.tar.gz"
    with tarfile.open(backup, "w:gz") as archive:
        for name in ("hooks.json", "config.toml"):
            file = home / name
            if file.is_file():
                archive.add(file, arcname=name)
    os.chmod(backup, 0o600)
    return backup


def _merge_hooks(file: Path, command: str | None, previous_command: str | None = None) -> bool:
    if file.is_symlink():
        raise DeliveryError("CODEX_HOOK_CONFIG_INVALID", "hooks.json の symlink は変更できません")
    current = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
    if not isinstance(current, dict) or not isinstance(current.get("hooks", {}), dict):
        raise DeliveryError("CODEX_HOOK_CONFIG_INVALID", "hooks.json の形式が不正です")
    next_value = dict(current)
    hooks = dict(current.get("hooks", {}))
    owned_commands = {value for value in (command, previous_command) if value is not None}
    for event in ("PostToolUse", "Stop"):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise DeliveryError("CODEX_HOOK_CONFIG_INVALID", f"{event} の形式が不正です")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise DeliveryError("CODEX_HOOK_CONFIG_INVALID", f"{event} の形式が不正です")
            entries = [entry for entry in group["hooks"] if not (
                isinstance(entry, dict) and entry.get("type") == "command" and
                isinstance(entry.get("command"), str) and
                entry["command"] in owned_commands
            )]
            if entries:
                kept.append({**group, "hooks": entries})
        if command is not None:
            entry: dict[str, Any] = {"type": "command", "command": command, "timeout": 20}
            group = {"hooks": [entry]}
            if event == "PostToolUse":
                group["matcher"] = ".*"
                entry["additionalContextLimit"] = 0
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    next_value["hooks"] = hooks
    if next_value == current:
        return False
    _write_json(file, next_value)
    return True


async def _verify_hooks(command: str, approve: bool) -> None:
    home = codex_home()
    async with CodexRPC(home) as rpc:
        async def owned() -> list[dict[str, Any]]:
            result = await rpc.request("hooks/list", {"cwds": [str(home)]})
            data = result.get("data")
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise DeliveryError("CODEX_HOOK_LIST_INVALID", "Codex の hook 一覧を認識できません")
            rows = data[0].get("hooks")
            if not isinstance(rows, list):
                raise DeliveryError("CODEX_HOOK_LIST_INVALID", "Codex の hook 一覧を認識できません")
            ours = [row for row in rows if isinstance(row, dict) and row.get("command") == command]
            if len(ours) != 2 or {row.get("eventName") for row in ours} != {"postToolUse", "stop"}:
                raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "登録した hook が Codex から見えません")
            return ours

        rows = await owned()
        if approve:
            edits = []
            for row in rows:
                if row.get("enabled") and row.get("trustStatus") in ("trusted", "managed"):
                    continue
                key, digest = row.get("key"), row.get("currentHash")
                if not isinstance(key, str) or not isinstance(digest, str):
                    raise DeliveryError("CODEX_HOOK_TRUST_INVALID", "hook の承認情報を認識できません")
                prefix = f"hooks.state.{json.dumps(key)}"
                edits.extend([
                    {"keyPath": f"{prefix}.trusted_hash", "value": digest, "mergeStrategy": "replace"},
                    {"keyPath": f"{prefix}.enabled", "value": True, "mergeStrategy": "replace"},
                ])
            if edits:
                await rpc.request("config/batchWrite", {
                    "edits": edits, "filePath": str(home / "config.toml"),
                })
            rows = await owned()
        if any(not row.get("enabled") or row.get("trustStatus") not in ("trusted", "managed") for row in rows):
            raise DeliveryError("CODEX_HOOK_UNTRUSTED", "hook が未承認です")


def _codex_mcp(*args: str) -> str:
    result = subprocess.run([codex_binary(), "mcp", *args], capture_output=True, text=True)
    if result.returncode:
        raise DeliveryError("CODEX_MCP_CONFIG_FAILED", result.stderr.strip() or "Codex MCP 設定に失敗しました")
    return result.stdout


def _existing(name: str) -> dict[str, Any]:
    value = json.loads(_codex_mcp("get", name, "--json"))
    if not isinstance(value, dict) or not isinstance(value.get("transport"), dict):
        raise DeliveryError("CODEX_MCP_CONFIG_INVALID", "既存 MCP 設定を認識できません")
    return value


def _find_existing() -> tuple[str, dict[str, Any]]:
    for name in _NAMES:
        try:
            return name, _existing(name)
        except DeliveryError as exc:
            if exc.code != "CODEX_MCP_CONFIG_FAILED":
                raise
    raise DeliveryError("CODEX_MCP_CONFIG_MISSING", "既存の call-bridge MCP 登録がありません")


def _remote(existing: dict[str, Any]) -> tuple[str, str]:
    transport = existing["transport"]
    url, token_env = transport.get("url"), transport.get("bearer_token_env_var")
    if transport.get("type") != "streamable_http" or not isinstance(url, str) or not isinstance(token_env, str):
        raise DeliveryError("CODEX_MCP_CONFIG_UNSUPPORTED", "既存の HTTP MCP と token 環境変数が必要です")
    return url, token_env


async def _replace_mcp(name: str, registration: dict[str, Any]) -> None:
    """対象MCPだけを公式の設定APIで置き換える。"""
    home = codex_home()
    async with CodexRPC(home) as rpc:
        for value in (None, registration):
            await rpc.request("config/batchWrite", {
                "edits": [{
                    "keyPath": f"mcp_servers.{name}",
                    "value": value,
                    "mergeStrategy": "replace",
                }],
                "filePath": str(home / "config.toml"),
            })


async def enable() -> dict[str, str]:
    name, existing = _find_existing()
    config_file = state_root() / "config.json"
    already_local = existing["transport"].get("type") == "stdio"
    if already_local:
        if "call_bridge.local" not in str(existing["transport"].get("args", [])):
            raise DeliveryError("CODEX_MCP_CONFIG_CONFLICT", "call-bridge は別のローカル MCP です")
        if not config_file.exists():
            raise DeliveryError("CODEX_MCP_CONFIG_UNSUPPORTED", "ローカル MCP の所有設定がありません")
        previous = json.loads(config_file.read_text(encoding="utf-8"))
        url, token_env = previous["mcp_url"], previous["token_env"]
    else:
        url, token_env = _remote(existing)
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise DeliveryError("BRIDGE_TOKEN_MISSING", f"{token_env} がありません")
    home = codex_home()
    command = _command()
    _backup_codex_config(home)
    changed = _merge_hooks(home / "hooks.json", command,
                           previous.get("hook_command") if already_local else None)
    await _verify_hooks(command, approve=True)
    auth_file = state_root() / "auth.json"
    auth_file_created = not auth_file.exists()
    _write_json(auth_file, {"token": token})
    if not already_local:
        try:
            await _replace_mcp(name, {"command": sys.executable, "args": ["-m", "call_bridge.local"]})
        except Exception:
            await _replace_mcp(name, {"url": url, "bearer_token_env_var": token_env})
            raise
    actual = _existing(name)
    if actual["transport"].get("type") != "stdio":
        raise DeliveryError("CODEX_MCP_CONFIG_INVALID", "ローカル MCP への切替を確認できません")
    _write_json(state_root() / "config.json", {
        "enabled": True,
        "mcp_name": name,
        "mcp_url": url, "token_env": token_env,
        "codex_binary": str(Path(codex_binary()).resolve()),
        "hook_command": command,
    })
    return {"status": "restart_required" if changed or not already_local or auth_file_created else "ready", "mcp": name}


async def status() -> dict[str, str]:
    config_file = state_root() / "config.json"
    if not config_file.exists():
        return {"status": "disabled"}
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if config.get("enabled") is False:
        return {"status": "disabled"}
    await _verify_hooks(config["hook_command"], approve=False)
    name = config["mcp_name"]
    existing = _existing(name)
    if existing["transport"].get("type") != "stdio":
        raise DeliveryError("CODEX_MCP_CONFIG_INVALID", "Codex はローカル MCP を使っていません")
    return {"status": "ready", "mcp": name, "remote": config["mcp_url"]}


async def disable() -> dict[str, str]:
    config_file = state_root() / "config.json"
    if not config_file.exists():
        return {"status": "disabled"}
    config = json.loads(config_file.read_text(encoding="utf-8"))
    name = config["mcp_name"]
    existing = _existing(name)
    transport = existing["transport"]
    if transport.get("type") != "stdio" or "call_bridge.local" not in str(transport.get("args", [])):
        raise DeliveryError("CODEX_MCP_CONFIG_CONFLICT", "call-bridge の登録が別製品へ変更されています")
    home = codex_home()
    _backup_codex_config(home)
    _merge_hooks(home / "hooks.json", None, config["hook_command"])
    try:
        await _replace_mcp(name, {"url": config["mcp_url"],
                                  "bearer_token_env_var": config["token_env"]})
    except Exception:
        await _replace_mcp(name, {"command": sys.executable, "args": ["-m", "call_bridge.local"]})
        raise
    _write_json(config_file, {**config, "enabled": False})
    (state_root() / "auth.json").unlink(missing_ok=True)
    return {"status": "restart_required", "mcp": name}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("enable", "status", "disable"), default="status", nargs="?")
    action = parser.parse_args().action
    try:
        result = asyncio.run(enable() if action == "enable" else disable() if action == "disable" else status())
        print(json.dumps(result, ensure_ascii=False))
    except DeliveryError as exc:
        print(json.dumps({"status": "failed", "error": exc.code, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
