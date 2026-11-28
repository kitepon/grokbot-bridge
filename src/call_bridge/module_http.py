"""One credentialed POST through the BellTeam authentication module (caller socket).

The module holds the URL and key; the bridge sends only the JSON body and gets the status back.
Contract: docs/architecture/module-webhooks.md
"""
from __future__ import annotations

import base64
import json
import os
import socket

SOCKET_ENV = "CALL_BRIDGE_AUTH_SOCKET"


class ModuleHttpError(Exception):
    """Short text only: never the URL, a key or a response body."""


def configured(item: str) -> bool:
    return bool(item and os.environ.get(SOCKET_ENV, "").strip())


def post_json(item: str, payload: bytes, timeout: float) -> int:
    request = {
        "op": "http", "name": item, "method": "POST", "path": "/",
        "headers": {"content-type": "application/json"},
        "body": base64.b64encode(payload).decode("ascii"),
        "timeout_sec": max(1, int(timeout)),
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout + 5)  # the module's own request limit comes first
            sock.connect(os.environ[SOCKET_ENV].strip())
            sock.sendall((json.dumps(request) + "\n").encode())
            with sock.makefile("rb") as reader:
                line = reader.readline(1 << 20)
        reply = json.loads(line)
    except (OSError, ValueError) as exc:
        raise ModuleHttpError(f"module unreachable: {type(exc).__name__}") from None
    if reply.get("outcome") != "granted":
        raise ModuleHttpError("module " + str(reply.get("outcome") or "no answer"))
    return int(reply["status"])
