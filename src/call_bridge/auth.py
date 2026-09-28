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
migration; once the file has entries its use logs a warning every 5 minutes.
With neither setting the bridge is open (dev). A named file must hold at least
one valid entry, or the bridge refuses to start.
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


# No token configured (development) or an in-process call: the current behavior.
OPEN = Principal(name="open", ops=True)
# A request that skipped authentication: no system matches it.
UNAUTHENTICATED = Principal(name="unauthenticated", system="")
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


def _flag(raw: dict[str, Any], key: str, name: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise AuthError(f"token entry {name}: {key} must be true or false")
    return value


def _text(raw: dict[str, Any], key: str, name: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AuthError(f"token entry {name}: {key} must be a non-empty string")
    return value.strip()


def _load_entries(path: str) -> list[_Entry]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise AuthError(f"CALL_BRIDGE_TOKENS_FILE cannot be read: {path}: {e.strerror or e}") from e
    except ValueError as e:
        raise AuthError(f"CALL_BRIDGE_TOKENS_FILE is not valid JSON: {path}: {e}") from e
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, list) or not tokens:
        raise AuthError(f"CALL_BRIDGE_TOKENS_FILE has no tokens: {path}")
    entries: list[_Entry] = []
    seen: set[bytes] = set()
    for index, raw in enumerate(tokens):
        if not isinstance(raw, dict):
            raise AuthError(f"token entry #{index} must be an object")
        name = _text(raw, "name", f"#{index}")
        if name is None:
            raise AuthError(f"token entry #{index} needs a name")
        digest_hex = (_text(raw, "sha256", name) or "").lower()
        try:
            digest = bytes.fromhex(digest_hex)
        except ValueError:
            digest = b""
        if len(digest) != 32:
            raise AuthError(f"token entry {name}: sha256 must be 64 hex characters")
        if digest in seen:
            raise AuthError(f"token entry {name}: the same sha256 appears twice")
        seen.add(digest)
        system = _text(raw, "system", name)
        if system is not None and system not in SYSTEMS:
            raise AuthError(f"token entry {name}: unknown system {system}")
        ops = _flag(raw, "ops", name)
        if system is None and not ops:
            raise AuthError(f"token entry {name}: an entry without system must set ops")
        entries.append(_Entry(
            name=name,
            digest=digest,
            system=system,
            id=_text(raw, "id", name),
            caller_id_header=_flag(raw, "caller_id_header", name),
            ops=ops,
        ))
    return entries


class Authenticator:
    def __init__(self, legacy_token: str = "", tokens_file: str = "") -> None:
        self.legacy_token = legacy_token.strip()
        self.tokens_file = tokens_file
        self.entries = _load_entries(tokens_file) if tokens_file else []
        self._last_legacy_warn: float | None = None

    @classmethod
    def from_env(cls) -> Authenticator:
        return cls(
            os.environ.get("CALL_BRIDGE_TOKEN", ""),
            os.environ.get("CALL_BRIDGE_TOKENS_FILE", "").strip(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.legacy_token or self.tokens_file)

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
            self._warn_legacy()
            return LEGACY
        return None

    def _warn_legacy(self) -> None:
        # Before the switch every client uses the shared token; warn only once
        # bound tokens exist and the shared one should be going away.
        if not self.entries:
            return
        now = time.monotonic()
        if self._last_legacy_warn is None or now - self._last_legacy_warn >= LEGACY_WARN_INTERVAL:
            self._last_legacy_warn = now
            log.warning("legacy shared CALL_BRIDGE_TOKEN in use; caller identity is not checked")

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
