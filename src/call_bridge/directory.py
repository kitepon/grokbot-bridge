"""Phone directory from Grok Bot seat profiles (name / title / description).

Clients call ``call_directory``. This module builds the book on that request.
There is no periodic sync and no post-edit push.

Source order:
1. ``CALL_BRIDGE_DIRECTORY_URL`` — HTTP GET (preferred when call-bridge is not
   on the box that holds the profiles).
2. Local ``profile.json`` files under ``CALL_BRIDGE_AGENTS_ROOT``, or
   ``/home/box/agent-data/agents`` when that env var is unset and the path exists.
3. ``directory.json`` — last-resort snapshot. Its ``source`` / ``agents_root``
   fields are not treated as a live read.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("call_bridge.directory")

SCHEMA = "grokbot.directory.v0"
DEFAULT_AGENTS_ROOT = Path("/home/box/agent-data/agents")
DIRECTORY_URL_TIMEOUT_SECONDS = 2.5
DIRECTORY_HOP_HEADER = "X-Call-Bridge-Directory-Hop"
_MAX_DIRECTORY_BYTES = 1_000_000

# Placeholders / retired seats — not a phone book.
_SKIP_NAMES = {"", "New Agent", "New Bot", "ゲスト"}


class DirectoryFetchError(Exception):
    """The directory URL did not return a usable book. ``detail`` is safe to surface."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    """Return 3xx/4xx/5xx as responses so Authorization is not replayed on a redirect."""

    def http_response(self, request, response):  # noqa: ARG002
        return response

    https_response = http_response


def _directory_url() -> str:
    return os.environ.get("CALL_BRIDGE_DIRECTORY_URL", "").strip()


def _directory_url_timeout() -> float:
    raw = os.environ.get("CALL_BRIDGE_DIRECTORY_URL_TIMEOUT", "").strip()
    if not raw:
        return DIRECTORY_URL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DIRECTORY_URL_TIMEOUT_SECONDS
    if value <= 0:
        return DIRECTORY_URL_TIMEOUT_SECONDS
    return value


def _authorization_value(raw: str) -> str:
    """Bearer token, or an Authorization header value that already includes the scheme."""
    value = raw.strip()
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value
    return f"Bearer {value}"


def _short(text: str, limit: int = 180) -> str:
    compact = " ".join(str(text).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def agents_root() -> Path | None:
    """Local profile tree, if one is configured and present.

    When ``CALL_BRIDGE_AGENTS_ROOT`` is set, only that path is used. The default
    box path is not a second guess — a copied tree on another host must not
    silently win over an explicit (but missing) configuration.
    """
    env = os.environ.get("CALL_BRIDGE_AGENTS_ROOT", "").strip()
    if env:
        path = Path(env)
        return path if path.is_dir() else None
    if DEFAULT_AGENTS_ROOT.is_dir():
        return DEFAULT_AGENTS_ROOT
    return None


def _snapshot_paths() -> list[Path]:
    env = os.environ.get("CALL_BRIDGE_DIRECTORY", "").strip()
    paths: list[Path] = []
    if env:
        paths.append(Path(env))
    paths.append(Path("directory.json"))
    paths.append(Path(__file__).resolve().parents[2] / "directory.json")
    return paths


def build_members_from_profiles(root: Path) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        pj = d / "profile.json"
        if not pj.is_file():
            continue
        try:
            p = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if name in _SKIP_NAMES:
            continue
        entry: dict[str, Any] = {"name": name}
        title = (p.get("title") or "").strip()
        role = (p.get("description") or "").strip()
        # Keep profile text as-is (only normalize newlines to spaces for one field).
        if title:
            entry["title"] = title
        if role:
            entry["role"] = " ".join(role.split())
        members.append(entry)

    def sort_key(m: dict[str, Any]) -> tuple[int, str]:
        return (0, m["name"]) if m["name"] == "マリアン" else (1, m["name"])

    members.sort(key=sort_key)
    return members


def build_directory_doc(root: Path | None = None) -> dict[str, Any]:
    root = root or agents_root()
    if root is None:
        raise FileNotFoundError("agents root not found")
    members = build_members_from_profiles(root)
    return {
        "schema": SCHEMA,
        "source": "agent-profiles",
        "agents_root": str(root),
        "members": members,
    }


def write_directory_snapshot(path: Path, root: Path | None = None) -> dict[str, Any]:
    doc = build_directory_doc(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


def _fetch_directory_url(url: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        DIRECTORY_HOP_HEADER: "1",
    }
    auth = _authorization_value(os.environ.get("CALL_BRIDGE_DIRECTORY_URL_AUTH", ""))
    if auth:
        headers["Authorization"] = auth
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=_directory_url_timeout()) as resp:
            code = int(getattr(resp, "status", 0) or resp.getcode())
            if code < 200 or code >= 300:
                raise DirectoryFetchError(f"http {code}")
            raw = resp.read(_MAX_DIRECTORY_BYTES + 1)
    except DirectoryFetchError:
        raise
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError)
        if timed_out:
            raise DirectoryFetchError("timed out") from exc
        raise DirectoryFetchError("request failed") from exc

    if len(raw) > _MAX_DIRECTORY_BYTES:
        raise DirectoryFetchError("directory response too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectoryFetchError("invalid directory json") from exc
    return _book_from_url_payload(url, data)


def _book_from_url_payload(url: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DirectoryFetchError("invalid directory json")
    if data.get("ok") is False:
        err = data.get("error") or "directory unavailable"
        raise DirectoryFetchError(_short(str(err)))
    members = data.get("members")
    if not isinstance(members, list) or any(not isinstance(item, dict) for item in members):
        raise DirectoryFetchError("invalid directory json")

    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        source = "directory-url"
    else:
        source = source.strip()

    out: dict[str, Any] = {
        "ok": True,
        "schema": data.get("schema") if isinstance(data.get("schema"), str) else SCHEMA,
        "source": source,
        "directory_url": url,
        "count": len(members),
        "members": members,
    }
    agents = data.get("agents_root")
    if isinstance(agents, str) and agents.strip():
        out["agents_root"] = agents.strip()
    return out


def _load_agents_root() -> dict[str, Any] | None:
    root = agents_root()
    if root is None:
        return None
    members = build_members_from_profiles(root)
    return {
        "ok": True,
        "schema": SCHEMA,
        "source": "agent-profiles",
        "agents_root": str(root),
        "count": len(members),
        "members": members,
    }


def _load_snapshot() -> dict[str, Any] | None:
    for path in _snapshot_paths():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        members = data.get("members")
        if members is None:
            members = []
        if not isinstance(members, list):
            continue
        # Never reuse a snapshot's source or agents_root. Those fields mean a
        # live profile read, and a saved file is not one.
        return {
            "ok": True,
            "path": str(path.resolve()),
            "schema": data.get("schema") if isinstance(data.get("schema"), str) else SCHEMA,
            "source": "directory.json",
            "count": len(members),
            "members": members,
        }
    return None


def _unavailable(url_error: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": "directory_unavailable",
        "detail": (
            "No live directory and no directory.json. "
            "Set CALL_BRIDGE_DIRECTORY_URL to the on-demand profile server, "
            "or CALL_BRIDGE_AGENTS_ROOT when profiles are on this host."
        ),
        "count": 0,
        "members": [],
    }
    if url_error:
        out["directory_url_error"] = url_error
    return out


def load_directory(*, skip_url: bool = False) -> dict[str, Any]:
    """Build the phone book for this request.

    ``skip_url`` is set when this process is already answering a directory GET
    that arrived with ``DIRECTORY_HOP_HEADER``, so a URL pointed at this same
    service cannot loop.
    """
    url_error: str | None = None
    if not skip_url:
        url = _directory_url()
        if url:
            try:
                return _fetch_directory_url(url)
            except DirectoryFetchError as exc:
                url_error = exc.detail
                log.warning("directory URL unavailable: %s", url_error)
            except Exception as exc:
                url_error = "request failed"
                log.warning("directory URL unavailable: %s", _short(f"{type(exc).__name__}: {exc}"))

    local = _load_agents_root()
    if local is not None:
        return local

    snap = _load_snapshot()
    if snap is not None:
        if url_error:
            snap["directory_url_error"] = url_error
        return snap
    return _unavailable(url_error)


def search_directory(query: str | None = None, *, skip_url: bool = False) -> dict[str, Any]:
    base = load_directory(skip_url=skip_url)
    if not base.get("ok"):
        return base
    q = (query or "").strip().lower()
    members = list(base["members"])
    if q:
        filtered = []
        for m in members:
            blob = " ".join(str(m.get(k) or "") for k in ("name", "title", "role")).lower()
            if q in blob:
                filtered.append(m)
        members = filtered
    out = {k: v for k, v in base.items() if k != "members"}
    out["query"] = query or ""
    out["count"] = len(members)
    out["members"] = members
    return out
