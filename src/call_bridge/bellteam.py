"""BellTeam directory and delivery over a mounted UNIX socket."""

from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Any


class BellTeamError(Exception):
    pass


class BellTeamOutcomeUnknown(BellTeamError):
    """The request may have reached BellTeam; repeating it could deliver twice."""


class _Connection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def socket_path() -> str:
    return os.environ.get("CALL_BRIDGE_BELLTEAM_UNIX", "").strip()


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    target = socket_path()
    if not target:
        raise BellTeamError("BellTeam socket not configured")
    delivery = payload is not None
    conn = _Connection(target, timeout=30 if delivery else 3)
    connected = False
    try:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        conn.connect()
        connected = True
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            if delivery:
                raise BellTeamOutcomeUnknown("BellTeam delivery receipt too large")
            raise BellTeamError("BellTeam response too large")
        if delivery and response.status >= 500:
            raise BellTeamOutcomeUnknown(f"BellTeam http {response.status} after delivery request")
        if response.status < 200 or response.status >= 300:
            raise BellTeamError(f"BellTeam http {response.status}")
        data = json.loads(raw.decode())
        if not isinstance(data, dict):
            if delivery:
                raise BellTeamOutcomeUnknown("BellTeam delivery receipt invalid")
            raise BellTeamError("BellTeam response invalid")
        return data
    except BellTeamError:
        raise
    except (OSError, TimeoutError, ValueError, http.client.HTTPException) as exc:
        if delivery and connected:
            raise BellTeamOutcomeUnknown("BellTeam delivery outcome unknown") from exc
        raise BellTeamError("BellTeam unavailable") from exc
    finally:
        conn.close()


def fetch_directory() -> list[dict[str, Any]]:
    data = _request("GET", "/v0/directory")
    members = data.get("members")
    if not isinstance(members, list) or any(not isinstance(item, dict) for item in members):
        raise BellTeamError("BellTeam directory invalid")
    result = []
    for item in members:
        ident = item.get("id")
        name = item.get("name")
        if not isinstance(ident, str) or not ident or not isinstance(name, str) or not name:
            raise BellTeamError("BellTeam directory invalid")
        result.append({**item, "system": "bellteam"})
    return result


def send_delivery(payload: dict[str, Any]) -> dict[str, Any]:
    result = _request("POST", "/v0/deliver", payload)
    if result.get("delivery") not in ("running", "steered"):
        raise BellTeamOutcomeUnknown("BellTeam delivery receipt invalid")
    return result
