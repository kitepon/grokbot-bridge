"""ローカル Codex 受信口と公式 hook を登録する。

継続型の親への Steer の hook は aiterm-steer-delivery の codex setup が登録する。
call-bridge が登録するのは codex exec の親へ返信を渡す UserPromptSubmit hook だけ。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any

from . import harness_setup, receiver
from .codex_delivery import (STEER_PROFILE, CodexRPC, DeliveryError, check_steer_version, codex_binary,
                             codex_command, codex_home, codex_processes, current_steer_cli, grant_user_access,
                             owned_hooks, private_dir, restart_required, state_root, steer_profile, steer_sync)

_NAMES = ("call-bridge", "grokbot-bridge")


def _command() -> str:
    if os.name == "nt":
        python = sys.executable.replace("'", "''")
        script = f"& '{python}' -m call_bridge.codex_delivery; exit $LASTEXITCODE"
        import base64
        return "pwsh.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(script.encode("utf-16le")).decode()
    return f"{shlex.quote(sys.executable)} -m call_bridge.codex_delivery"


def _write_json(file: Path, value: dict[str, Any]) -> None:
    private_dir(file.parent)
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


_HOOK_EVENTS = ("PostToolUse", "Stop", "UserPromptSubmit")


def _plan_hooks(file: Path, command: str | None,
                previous_command: str | None = None
                ) -> tuple[dict[str, Any], dict[str, Any], list[tuple], list[tuple[str, tuple[int, int]]]]:
    """自製品の hook を置き換えた後の hooks.json と、位置が動く他の hook、自製品の hook が居た位置を計算する。"""
    if file.is_symlink():
        raise DeliveryError("CODEX_HOOK_CONFIG_INVALID", "hooks.json の symlink は変更できません")
    current = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
    if not isinstance(current, dict) or not isinstance(current.get("hooks", {}), dict):
        raise DeliveryError("CODEX_HOOK_CONFIG_INVALID", "hooks.json の形式が不正です")
    next_value = dict(current)
    hooks = dict(current.get("hooks", {}))
    owned_commands = {value for value in (command, previous_command) if value is not None}
    moves: list[tuple] = []
    owned: list[tuple[str, tuple[int, int]]] = []
    for event in _HOOK_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise DeliveryError("CODEX_HOOK_CONFIG_INVALID", f"{event} の形式が不正です")
        before: list[tuple[Any, tuple[int, int]]] = []
        kept = []
        insertion: int | None = None
        for g, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise DeliveryError("CODEX_HOOK_CONFIG_INVALID", f"{event} の形式が不正です")
            entries = [entry for entry in group["hooks"] if not (
                isinstance(entry, dict) and entry.get("type") == "command" and
                isinstance(entry.get("command"), str) and
                entry["command"] in owned_commands
            )]
            before.extend((entry, (g, h)) for h, entry in enumerate(group["hooks"]) if any(entry is e for e in entries))
            owned.extend((event, (g, h)) for h, entry in enumerate(group["hooks"]) if not any(entry is e for e in entries))
            if len(entries) != len(group["hooks"]) and insertion is None:
                insertion = len(kept)
            if entries:
                kept.append({**group, "hooks": entries})
        # 旧版が登録した PostToolUse／Stop は外すだけ。Steer はパッケージの hook が受け持つ。
        # 自分の hook は元の位置に置く。末尾へ移すと、後ろの hook の位置と承認がずれる。
        if command is not None and event == "UserPromptSubmit":
            kept.insert(len(kept) if insertion is None else insertion,
                        {"hooks": [{"type": "command", "command": command, "timeout": 20,
                                    "additionalContextLimit": 0}]})
        after = [(entry, (g, h)) for g, group in enumerate(kept) for h, entry in enumerate(group["hooks"])]
        for entry, source in before:
            target = next((position for other, position in after if other is entry), None)
            if target != source:
                moves.append((event, source, target))
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    next_value["hooks"] = hooks
    return current, next_value, moves, owned


def _snake(event: str) -> str:
    return "".join(f"_{char.lower()}" if char.isupper() and index else char.lower()
                   for index, char in enumerate(event))


async def _merge_hooks(file: Path, command: str | None, previous_command: str | None = None) -> bool:
    """hooks.json を書き換える。位置が動く他の hook は、Codex が位置の鍵で持つ承認を新しい位置へ写す。

    承認を新たに与えたり外したりはしない（aiterm-steer-delivery 0.1.1 と同じ扱い）。
    """
    current, next_value, moves, owned = _plan_hooks(file, command, previous_command)
    if next_value == current:
        return False
    if not moves:
        _write_json(file, next_value)
        if owned:
            # 外した位置の承認記録の後片付けができなくても、hook の書き換えそのものは止めない。
            try:
                await _clean_vacated_trust(file, next_value, owned)
            except Exception as exc:
                print(f"call-bridge: 外した hook の承認記録を消せませんでした（{exc}）", file=sys.stderr)
        return True
    home = file.parent
    async with CodexRPC(home) as rpc:
        listed = await rpc.request("hooks/list", {"cwds": [str(home)]})
        rows = listed.get("data")
        source = str(file.resolve())
        sample = next((row for row in (rows[0].get("hooks", []) if isinstance(rows, list) and rows and
                                       isinstance(rows[0], dict) else [])
                       if isinstance(row, dict) and row.get("sourcePath") == source and
                       isinstance(row.get("key"), str)), None)
        if sample is None:
            raise DeliveryError("CODEX_HOOK_LIST_INVALID", "hook の承認の鍵を確認できません")
        prefix = ":".join(sample["key"].split(":")[:-3])

        def key(event: str, position: tuple[int, int]) -> str:
            return f"{prefix}:{_snake(event)}:{position[0]}:{position[1]}"

        # 鍵の形は Codex の内部に頼っている。移す前の鍵が公式 API に無ければ、何も書かずに止める。
        listed_keys = {row.get("key") for row in rows[0].get("hooks", []) if isinstance(row, dict)}
        if any(key(event, old) not in listed_keys for event, old, _new in moves):
            raise DeliveryError("CODEX_HOOK_LIST_INVALID", "hook の承認の鍵の形を確認できません。hooks.json は変えていません")

        config = (await rpc.request("config/read", {"includeLayers": False})).get("config") or {}
        state = (config.get("hooks") or {}).get("state") or {}
        config_file = str(home / "config.toml")
        edits = []
        for event, old, new in moves:
            if new is None:
                continue
            saved, target = state.get(key(event, old)), key(event, new)
            if isinstance(saved, dict):
                edits += [{"keyPath": f"hooks.state.{json.dumps(target)}.trusted_hash",
                           "value": saved.get("trusted_hash"), "mergeStrategy": "replace"},
                          {"keyPath": f"hooks.state.{json.dumps(target)}.enabled",
                           "value": saved.get("enabled"), "mergeStrategy": "replace"}]
            else:
                edits.append({"keyPath": f"hooks.state.{json.dumps(target)}", "value": None,
                              "mergeStrategy": "replace"})
        if edits:
            await rpc.request("config/batchWrite", {"edits": edits, "filePath": config_file})
        _write_json(file, next_value)
        used = {key(event, (g, h)) for event in _HOOK_EVENTS
                for g, group in enumerate(next_value["hooks"].get(event, []))
                for h, _entry in enumerate(group["hooks"])}
        vacated = ({key(event, old) for event, old, _new in moves} | {key(event, at) for event, at in owned}) - used
        vacated = [name for name in sorted(vacated) if name in state]
        if vacated:
            await rpc.request("config/batchWrite", {"edits": [
                {"keyPath": f"hooks.state.{json.dumps(name)}", "value": None, "mergeStrategy": "replace"}
                for name in vacated], "filePath": config_file})
    return True


def _used_positions(next_value: dict[str, Any]) -> set[tuple[str, int, int]]:
    return {(_snake(event), g, h) for event in _HOOK_EVENTS
            for g, group in enumerate(next_value["hooks"].get(event, []))
            for h, _entry in enumerate(group["hooks"])}


def _same_path(stored: str, source: Path) -> bool:
    if stored == str(source):
        return True
    try:
        return Path(stored).resolve() == source
    except OSError:
        return os.name == "nt" and stored.lower() == str(source).lower()


async def _clean_vacated_trust(file: Path, next_value: dict[str, Any],
                               owned: list[tuple[str, tuple[int, int]]]) -> None:
    """自製品の hook が居て、ほかの hook が入らずに空いた位置の承認記録を消す（aiterm-steer-delivery 0.1.4 と同じ）。"""
    wanted = {(_snake(event), g, h) for event, (g, h) in owned} - _used_positions(next_value)
    if not wanted:
        return
    source = file.resolve()
    async with CodexRPC(file.parent) as rpc:
        config = (await rpc.request("config/read", {"includeLayers": False})).get("config") or {}
        state = (config.get("hooks") or {}).get("state") or {}
        vacated = []
        for stored in state:
            parts = stored.rsplit(":", 3)
            if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
                continue
            if (parts[1], int(parts[2]), int(parts[3])) in wanted and _same_path(parts[0], source):
                vacated.append(stored)
        if vacated:
            await rpc.request("config/batchWrite", {"edits": [
                {"keyPath": f"hooks.state.{json.dumps(name)}", "value": None, "mergeStrategy": "replace"}
                for name in vacated], "filePath": str(file.parent / "config.toml")})


async def _verify_hooks(command: str, approve: bool) -> None:
    home = codex_home()
    async with CodexRPC(home) as rpc:
        async def owned() -> list[dict[str, Any]]:
            result = await rpc.request("hooks/list", {"cwds": [str(home)]})
            return owned_hooks(result, command, home)

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
    result = subprocess.run([*codex_command(), "mcp", *args], capture_output=True, text=True)
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


def _steer_runtime() -> dict[str, str | None]:
    path = current_steer_cli({})
    if path.suffix.lower() not in (".js", ".mjs"):
        raise DeliveryError("STEER_DELIVERY_UNAVAILABLE", f"aiterm-steer-delivery の dist/cli.js を特定できません: {path}")
    check_steer_version(path)
    node = shutil.which("node")
    if not node:
        raise DeliveryError("STEER_DELIVERY_UNAVAILABLE", "aiterm-steer-delivery の Node 実行ファイルが見つかりません")
    return {"steer_cli": str(path), "steer_node": str(Path(node).resolve())}


def _steer_setup(action: str, config: dict[str, Any]) -> str:
    """パッケージの Steer hook を操作する。パッケージが Codex を見つけられない環境は unsupported を返す。"""
    result = steer_sync(["setup", action], config)
    status = result.get("status")
    if status not in ("ready", "disabled", "restart_required", "unsupported"):
        raise DeliveryError("CODEX_STEER_SETUP_FAILED", str(result.get("reason_code") or status))
    return status


async def enable() -> dict[str, str]:
    name, existing = _find_existing()
    # 設定を書き換える前に、Steer の配送に使うパッケージが呼べることを確かめる。
    steer_runtime = _steer_runtime()
    running_before = codex_processes(codex_home())
    config_file = state_root() / "config.json"
    already_local = existing["transport"].get("type") == "stdio"
    if already_local:
        if "call_bridge.local" not in str(existing["transport"].get("args", [])):
            raise DeliveryError("CODEX_MCP_CONFIG_CONFLICT", "call-bridge は別のローカル MCP です")
        if not config_file.exists():
            raise DeliveryError("CODEX_MCP_CONFIG_UNSUPPORTED", "ローカル MCP の所有設定がありません")
        previous = json.loads(config_file.read_text(encoding="utf-8"))
        if previous.get("codex_home") and Path(previous["codex_home"]).resolve() != codex_home():
            raise DeliveryError("CODEX_HOOK_HOME_MISMATCH", "設定済みの Codex 環境が異なります")
        url, token_env = previous["mcp_url"], previous["token_env"]
    else:
        url, token_env = _remote(existing)
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise DeliveryError("BRIDGE_TOKEN_MISSING", f"{token_env} がありません")
    home = codex_home()
    command = _command()
    _backup_codex_config(home)
    changed = await _merge_hooks(home / "hooks.json", command,
                           previous.get("hook_command") if already_local else None)
    await _verify_hooks(command, approve=True)
    auth_file = state_root() / "auth.json"
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
    binary_path = Path(codex_binary()).resolve()
    node_path = shutil.which("node") if binary_path.suffix.lower() == ".js" else None
    if binary_path.suffix.lower() == ".js" and not node_path:
        raise DeliveryError("CODEX_RUNTIME_UNAVAILABLE", "Codex の Node 実行ファイルが見つかりません")
    next_config = {
        "enabled": True,
        "mcp_name": name,
        "mcp_url": url, "token_env": token_env,
        "codex_home": str(home),
        "codex_binary": str(binary_path),
        "node_binary": str(Path(node_path).resolve()) if node_path else None,
        "hook_command": command,
        "stale_processes": (running_before if changed or not already_local or
                            "stale_processes" not in previous else previous["stale_processes"]),
        # 止まった会話の返信を渡す席は、Aiterm が立てる。無い端末では、立てる時に AITERM_UNAVAILABLE で知らせる。
        "aiterm_mcp": shutil.which("aiterm-mcp"),
        **steer_runtime,
    }
    _write_json(state_root() / STEER_PROFILE, steer_profile(name))
    _write_json(state_root() / "config.json", next_config)
    steer = _steer_setup("enable", next_config)
    # ここまでに書いた物を、この利用者の普段の権限の process（Codex・Claude Code・常駐）が開けるようにする。
    grant_user_access()
    restart = restart_required(next_config) or steer == "restart_required"
    return {"status": "restart_required" if restart else "ready", "mcp": name, "steer": steer,
            "receiver": _receiver("install")}


def _receiver(action: str) -> str:
    """常駐の受け取り係。登録できない環境（常駐の仕組みが無いコンテナなど）でも、Codex の設定は通す。"""
    try:
        state = getattr(receiver, action)() if action != "status" else receiver.state()
    except DeliveryError as exc:
        return f"failed: {exc}"
    if state.get("running"):
        return "running"
    return "registered" if state.get("registered") else "not_registered"


async def status() -> dict[str, str]:
    config_file = state_root() / "config.json"
    if not config_file.exists():
        return {"status": "disabled"}
    config = json.loads(config_file.read_text(encoding="utf-8"))
    if config.get("enabled") is False:
        return {"status": "disabled"}
    if config.get("codex_home") and Path(config["codex_home"]).resolve() != codex_home():
        raise DeliveryError("CODEX_HOOK_HOME_MISMATCH", "設定済みの Codex 環境が異なります")
    await _verify_hooks(config["hook_command"], approve=False)
    name = config["mcp_name"]
    existing = _existing(name)
    if existing["transport"].get("type") != "stdio":
        raise DeliveryError("CODEX_MCP_CONFIG_INVALID", "Codex はローカル MCP を使っていません")
    if not (state_root() / STEER_PROFILE).is_file():
        raise DeliveryError("CODEX_HOOK_UNAVAILABLE", "call-bridge-setup enable を再実行してください")
    check_steer_version(current_steer_cli(config))
    steer = _steer_setup("status", config)
    restart = restart_required(config) or steer == "restart_required"
    return {"status": "restart_required" if restart else "ready",
            "mcp": name, "remote": config["mcp_url"], "steer": steer, "receiver": _receiver("status")}


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
    # CLI が消えていても Node を入れ替えていても、call-bridge の設定は元へ戻す。
    warning = None
    if (state_root() / STEER_PROFILE).is_file():
        try:
            _steer_setup("disable", config)
        except DeliveryError as exc:
            warning = (f"Steer の hook を外せませんでした（{exc}）。aiterm-steer-delivery を入れ直して "
                       f"aiterm-steer-delivery --profile {state_root() / STEER_PROFILE} codex setup disable を実行してください")
    home = codex_home()
    _backup_codex_config(home)
    await _merge_hooks(home / "hooks.json", None, config["hook_command"])
    try:
        await _replace_mcp(name, {"url": config["mcp_url"],
                                  "bearer_token_env_var": config["token_env"]})
    except Exception:
        await _replace_mcp(name, {"command": sys.executable, "args": ["-m", "call_bridge.local"]})
        raise
    _write_json(config_file, {**config, "enabled": False})
    (state_root() / "auth.json").unlink(missing_ok=True)
    result = {"status": "restart_required", "mcp": name, "receiver": _receiver("uninstall")}
    return {**result, "warning": warning} if warning else result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="call-bridge の端末の側を設定する。引数なしは Codex の status。")
    parser.add_argument("action", choices=("enable", "status", "disable", "receiver", "harness"),
                        default="status", nargs="?")
    parser.add_argument("target", nargs="?",
                        help="receiver: install／status／uninstall。harness: claude-code／cursor／grok")
    parser.add_argument("operation", nargs="?", choices=("enable", "status", "disable"), default="status",
                        help="harness の時の操作")
    args = parser.parse_args()
    try:
        if args.action == "receiver":
            operation = args.target or "status"
            if operation not in ("install", "status", "uninstall"):
                parser.error("receiver は install／status／uninstall のどれかです")
            result: dict[str, Any] = {"receiver": _receiver(operation)}
        elif args.action == "harness":
            if args.target not in harness_setup.HARNESSES:
                parser.error("harness は claude-code／cursor／grok のどれかです")
            result = getattr(harness_setup, args.operation)(args.target)
        else:
            result = asyncio.run(enable() if args.action == "enable" else disable() if args.action == "disable"
                                 else status())
        print(json.dumps(result, ensure_ascii=False))
    except DeliveryError as exc:
        print(json.dumps({"status": "failed", "error": exc.code, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
