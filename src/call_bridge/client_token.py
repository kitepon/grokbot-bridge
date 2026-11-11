"""The terminal's bridge token from a command (a secret store), kept in process memory only.

Optional: without CALL_BRIDGE_TOKEN_COMMAND / config.json token_command the client keeps using the
environment variable or auth.json. See docs/architecture/client-token.md.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from .codex_delivery import DeliveryError, hidden_child

# Token command (CALL_BRIDGE_TOKEN_COMMAND or config.json token_command) -> token, process memory only.
_COMMAND_TOKENS: dict[str, str] = {}
# A desktop secret store may wait for a person to unlock it.
_TOKEN_COMMAND_SECONDS = 120


def token_command(config: dict[str, Any]) -> str:
    command = os.environ.get("CALL_BRIDGE_TOKEN_COMMAND", "").strip() or config.get("token_command") or ""
    if not isinstance(command, str):
        raise DeliveryError("LOCAL_CONFIG_INVALID", "token_command は文字列にしてください")
    return command.strip()


def command_token(command: str) -> str:
    """Run the token command once per process; the output never reaches disk, errors or logs."""
    token = _COMMAND_TOKENS.get(command)
    if token:
        return token
    try:
        done = subprocess.run(command, shell=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=_TOKEN_COMMAND_SECONDS, **hidden_child())
    except subprocess.TimeoutExpired:
        raise DeliveryError("BRIDGE_TOKEN_COMMAND_FAILED", "合言葉を出すコマンドが時間内に終わりません") from None
    except OSError as exc:
        raise DeliveryError("BRIDGE_TOKEN_COMMAND_FAILED",
                            f"合言葉を出すコマンドを起動できません（{exc.errno or 'os'}）") from None
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or len(lines) != 1 or not lines[0].strip():
        raise DeliveryError("BRIDGE_TOKEN_COMMAND_FAILED",
                            f"合言葉を出すコマンドが1行の値を返しません（exit {done.returncode}）")
    _COMMAND_TOKENS[command] = lines[0].strip()
    return _COMMAND_TOKENS[command]


def forget_token() -> bool:
    """After a 401: the next request runs the token command again. True when a command token was cached."""
    cached = bool(_COMMAND_TOKENS)
    _COMMAND_TOKENS.clear()
    return cached
