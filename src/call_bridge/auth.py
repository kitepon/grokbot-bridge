"""Caller identity: bind each bearer token to the system that uses it.

``CALL_BRIDGE_TOKENS_FILE`` maps token hashes to principals::

    {"tokens": [
      {"name": "bellteam", "sha256": "<hex>", "system": "bellteam", "caller_id_header": true},
      {"name": "grokbot", "sha256": "<hex>", "system": "grokbot"},
      {"name": "macbook", "sha256": "<hex>", "system": "local"},
      {"name": "ops", "sha256": "<hex>", "ops": true}
    ]}

A principal with ``system`` may act only as a party of that system in a call.
``caller_id_header`` lets that system's infrastructure name the exact caller in
``X-Call-Bridge-Caller-Id`` (BellTeam sets it per Bot). Without the header the
principal is system-wide. An entry without ``system`` is unrestricted and needs
``ops: true``.

``CALL_BRIDGE_TOKEN`` stays accepted as an unrestricted legacy token during
migration and logs a warning. With neither setting the bridge is open (dev).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("call_bridge.auth")

CALLER_ID_HEADER = "x-call-bridge-caller-id"
SYSTEMS = ("local", "grokbot", "bellteam")
LEGACY_WARN_INTERVAL = 300.0


@dataclass(frozen=True)
class Principal:
    name: str
    system: str | None = None
    id: str | None = None
    ops: bool = False

    @property
    def unrestricted(self) -> bool:
        return self.system is None


OPEN = Principal(name="open")
LEGACY = Principal(name="legacy", ops=True)


class AuthError(Exception):
    """Invalid token configuration or header."""


@dataclass(frozen=True)
class _Entry:
    name: str
    digest: bytes
    system: str | None
    id: str | None
    caller_id_header: bool
    ops: bool


def _load_entries(path: str) -> list[_Entry]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries: list[_Entry] = []
    for raw in data.get("tokens", []):
        name = str(raw.get("name") or "").strip()
        digest_hex = str(raw.get("sha256") or "").strip().lower()
        system = raw.get("system")
        if not name or len(digest_hex) != 64:
            raise AuthError(f"token entry needs name and sha256: {name or '?'}")
        if system is not None and system not in SYSTEMS:
            raise AuthError(f"unknown system in token entry {name}: {system}")
        if system is None and not raw.get("ops"):
            raise AuthError(f"token entry {name} without system must set ops")
        entries.append(_Entry(
            name=name,
            digest=bytes.fromhex(digest_hex),
            system=system,
            id=(str(raw["id"]).strip() or None) if raw.get("id") else None,
            caller_id_header=bool(raw.get("caller_id_header")),
            ops=bool(raw.get("ops")),
        ))
    return entries


class Authenticator:
    def __init__(self, legacy_token: str = "", tokens_file: str = "") -> None:
        self.legacy_token = legacy_token.strip()
        self.entries = _load_entries(tokens_file) if tokens_file else []
        self._last_legacy_warn = 0.0

    @classmethod
    def from_env(cls) -> Authenticator:
        return cls(
            os.environ.get("CALL_BRIDGE_TOKEN", ""),
            os.environ.get("CALL_BRIDGE_TOKENS_FILE", "").strip(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.legacy_token or self.entries)

    def authenticate(self, headers: Any) -> Principal | None:
        """Return the caller's principal, or None when the token is wrong."""
        if not self.enabled:
            return OPEN
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[len("Bearer "):]
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for entry in self.entries:
            if hmac.compare_digest(digest, entry.digest):
                return self._principal(entry, headers.get(CALLER_ID_HEADER, ""))
        if self.legacy_token and hmac.compare_digest(token.encode("utf-8"), self.legacy_token.encode("utf-8")):
            now = time.monotonic()
            if now - self._last_legacy_warn >= LEGACY_WARN_INTERVAL:
                self._last_legacy_warn = now
                log.warning("legacy shared CALL_BRIDGE_TOKEN in use; caller identity is not checked")
            return LEGACY
        return None

    @staticmethod
    def _principal(entry: _Entry, header_id: str) -> Principal:
        caller_id = entry.id
        header_id = header_id.strip()
        if header_id:
            if not entry.caller_id_header:
                raise AuthError(f"token {entry.name} may not set {CALLER_ID_HEADER}")
            if caller_id and header_id != caller_id:
                raise AuthError(f"{CALLER_ID_HEADER} does not match token {entry.name}")
            caller_id = header_id
        return Principal(name=entry.name, system=entry.system, id=caller_id, ops=entry.ops)


def is_party(principal: Principal, sess: dict[str, Any], party: str) -> bool:
    """Whether ``principal`` may act as ``party`` (local/member) of ``sess``."""
    if principal.unrestricted:
        return True
    if party == "local":
        system, party_id = sess.get("local_system") or "local", sess.get("local_id")
    elif party == "member":
        system, party_id = sess.get("member_system") or "grokbot", sess.get("member_id")
    else:
        return False
    return system == principal.system and (principal.id is None or party_id == principal.id)


def is_participant(principal: Principal, sess: dict[str, Any]) -> bool:
    return is_party(principal, sess, "local") or is_party(principal, sess, "member")


def check_open(principal: Principal, local_system: str, local_id: str) -> str | None:
    """Return why ``principal`` may not open a call as this local, or None."""
    if principal.unrestricted:
        return None
    if local_system != principal.system:
        return f"this connection calls as local_system={principal.system}"
    if principal.id is not None and local_id != principal.id:
        return f"this connection calls as local_id={principal.id}"
    return None
