"""Phone directory from Grok Bot seat profiles (name / title / description).

Each member built from a seat profile includes ``id``, the directory name
of that seat. That name is the Grok Bot agent id (``profile.json`` has no
id field). Remote books pass ``id`` / ``agentId`` through.

Clients call ``call_directory``. This module builds the book on that request.
A seat profile edit is visible on the next call. There is no periodic sync
and no post-edit push.

Source order:
1. ``CALL_BRIDGE_DIRECTORY_UNIX`` — HTTP GET over an ``AF_UNIX`` socket
   (path from ``CALL_BRIDGE_DIRECTORY_UNIX_PATH``, default ``/v0/directory``).
   Preferred when set. Prod mounts the socket's parent directory at
   ``/run/dirlive`` and sets this to ``/run/dirlive/dirlive.sock``.
2. ``CALL_BRIDGE_DIRECTORY_URL`` — HTTP GET, used when the unix socket is unset
   or that GET fails.
3. Local ``profile.json`` files, only if every configured remote GET failed
   (or none is configured): ``CALL_BRIDGE_AGENTS_ROOT``, or
   ``/home/box/agent-data/agents`` when that env var is unset and the path exists.
4. ``directory.json`` — last-resort snapshot. Its ``source`` / ``agents_root``
   fields are not treated as a live read.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import urllib.request
from pathlib import Path
from typing import Any

from .wake import LINK_DOWN_NOTE, notify_link_down

log = logging.getLogger("call_bridge.directory")

SCHEMA = "grokbot.directory.v0"
DEFAULT_AGENTS_ROOT = Path("/home/box/agent-data/agents")
DIRECTORY_URL_TIMEOUT_SECONDS = 2.5
DIRECTORY_UNIX_HTTP_PATH = "/v0/directory"
DIRECTORY_HOP_HEADER = "X-Call-Bridge-Directory-Hop"
_MAX_DIRECTORY_BYTES = 1_000_000

# Placeholders / retired seats — not a phone book.
_SKIP_NAMES = {"", "New Agent", "New Bot", "ゲスト"}

# The unix socket never produced an HTTP response. A status code means the
# socket accepted the connection, so that path is not a down tunnel.
_UNIX_LINK_DOWN_DETAILS = frozenset({
    "unix socket not found",
    "timed out",
    "request failed",
})


def is_unix_link_down(detail: str | None) -> bool:
    """True when the directory unix socket could not be reached."""
    return detail in _UNIX_LINK_DOWN_DETAILS


class DirectoryFetchError(Exception):
    """A remote directory GET did not return a usable book. ``detail`` is safe to surface."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    """Return 3xx/4xx/5xx as responses so Authorization is not replayed on a redirect."""

    def http_response(self, request, response):  # noqa: ARG002
        return response

    https_response = http_response


def _directory_unix_socket() -> str:
    return os.environ.get("CALL_BRIDGE_DIRECTORY_UNIX", "").strip()


def _directory_unix_http_path() -> str:
    raw = os.environ.get("CALL_BRIDGE_DIRECTORY_UNIX_PATH", "").strip()
    if not raw:
        return DIRECTORY_UNIX_HTTP_PATH
    return raw if raw.startswith("/") else f"/{raw}"


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
        # Grok Bot's agent id is the seat directory name. profile.json itself
        # has name / title / description only; local call_send needs the id
        # on the directory entry for the session.message relay.
        entry: dict[str, Any] = {"name": name, "id": d.name}
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


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 over an AF_UNIX stream socket. Does not follow redirects."""

    def __init__(self, unix_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix_path = unix_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._unix_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _remote_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Host": "localhost",
        DIRECTORY_HOP_HEADER: "1",
    }
    auth = _authorization_value(os.environ.get("CALL_BRIDGE_DIRECTORY_URL_AUTH", ""))
    if auth:
        headers["Authorization"] = auth
    return headers


def _failure_detail(exc: Exception) -> str:
    if isinstance(exc, DirectoryFetchError):
        return exc.detail
    reason = getattr(exc, "reason", exc)
    timed_out = isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError)
    if timed_out:
        return "timed out"
    return "request failed"


def _read_remote_body(raw: bytes, data_error: str = "invalid directory json") -> Any:
    if len(raw) > _MAX_DIRECTORY_BYTES:
        raise DirectoryFetchError("directory response too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectoryFetchError(data_error) from exc


def _fetch_directory_unix(sock_path: str) -> dict[str, Any]:
    http_path = _directory_unix_http_path()
    conn = _UnixHTTPConnection(sock_path, _directory_url_timeout())
    try:
        try:
            conn.request("GET", http_path, headers=_remote_headers())
            resp = conn.getresponse()
            code = int(resp.status)
            if code < 200 or code >= 300:
                raise DirectoryFetchError(f"http {code}")
            raw = resp.read(_MAX_DIRECTORY_BYTES + 1)
        except DirectoryFetchError:
            raise
        except FileNotFoundError as exc:
            raise DirectoryFetchError("unix socket not found") from exc
        except NotADirectoryError as exc:
            raise DirectoryFetchError("unix socket not found") from exc
        except Exception as exc:
            raise DirectoryFetchError(_failure_detail(exc)) from exc
    finally:
        conn.close()
    data = _read_remote_body(raw)
    return _book_from_remote_payload(data, directory_unix=sock_path, default_source="directory-unix")


def _fetch_directory_url(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_remote_headers(), method="GET")
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
        raise DirectoryFetchError(_failure_detail(exc)) from exc
    data = _read_remote_body(raw)
    return _book_from_remote_payload(data, directory_url=url, default_source="directory-url")


def _book_from_remote_payload(
    data: Any,
    *,
    directory_url: str | None = None,
    directory_unix: str | None = None,
    default_source: str,
) -> dict[str, Any]:
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
        source = default_source
    else:
        source = source.strip()

    out: dict[str, Any] = {
        "ok": True,
        "schema": data.get("schema") if isinstance(data.get("schema"), str) else SCHEMA,
        "source": source,
        "count": len(members),
        "members": members,
    }
    if directory_unix:
        out["directory_unix"] = directory_unix
    if directory_url:
        out["directory_url"] = directory_url
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


def _note_remote_errors(
    book: dict[str, Any],
    unix_error: str | None,
    url_error: str | None,
) -> dict[str, Any]:
    if unix_error:
        book["directory_unix_error"] = unix_error
    if url_error:
        book["directory_url_error"] = url_error
    return book


def _try_remote(kind: str, fetch: Any) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return fetch(), None
    except Exception as exc:
        detail = _failure_detail(exc)
        log.warning("directory %s unavailable: %s", kind, detail)
        return None, detail


def _unavailable(unix_error: str | None, url_error: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": "directory_unavailable",
        "detail": (
            "No live directory and no directory.json. "
            "Set CALL_BRIDGE_DIRECTORY_UNIX (or CALL_BRIDGE_DIRECTORY_URL) "
            "to the on-demand profile server, or CALL_BRIDGE_AGENTS_ROOT "
            "when profiles are on this host."
        ),
        "count": 0,
        "members": [],
    }
    return _note_remote_errors(out, unix_error, url_error)


def _note_unix_link_down(book: dict[str, Any], detail: str | None) -> dict[str, Any]:
    """Keep the book this request already built, and say the socket is down.

    The wake is best-effort and rate-limited inside ``notify_link_down``.
    A URL or file fallback is unchanged apart from this note.
    """
    if not detail:
        return book
    notify_link_down("directory", detail)
    book["note"] = LINK_DOWN_NOTE
    return book


def load_directory(*, skip_url: bool = False) -> dict[str, Any]:
    """Build the phone book for this request.

    ``skip_url`` is set when this process is already answering a directory GET
    that arrived with ``DIRECTORY_HOP_HEADER``, so a remote pointed at this
    same service cannot loop. It skips both the unix socket and the URL.
    """
    unix_error: str | None = None
    url_error: str | None = None
    unix_down: str | None = None
    if not skip_url:
        unix_sock = _directory_unix_socket()
        if unix_sock:
            book, unix_error = _try_remote("unix", lambda: _fetch_directory_unix(unix_sock))
            if book is not None:
                return book
            if is_unix_link_down(unix_error):
                unix_down = unix_error
        url = _directory_url()
        if url:
            book, url_error = _try_remote("URL", lambda: _fetch_directory_url(url))
            if book is not None:
                return _note_unix_link_down(book, unix_down)

    # Local profiles and directory.json are a soft fallback after remote failure
    # (or when no remote is configured). A successful remote GET does not reach here.
    local = _load_agents_root()
    if local is not None:
        return _note_unix_link_down(local, unix_down)

    snap = _load_snapshot()
    if snap is not None:
        return _note_unix_link_down(_note_remote_errors(snap, unix_error, url_error), unix_down)
    return _note_unix_link_down(_unavailable(unix_error, url_error), unix_down)


def _agent_id_of(member: dict[str, Any]) -> str:
    """Agent id carried on a directory entry.

    Profiles we build set ``id`` to the seat directory name. A remote book
    may use ``id`` or ``agentId``; both are the Grok Bot agent id.
    """
    for key in ("id", "agentId", "agent_id"):
        value = member.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _resolve_failure(book: dict[str, Any], error: str, detail: str) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": error, "detail": detail}
    note = book.get("note")
    if isinstance(note, str) and note and note not in detail:
        out["note"] = note
        out["detail"] = f"{detail}. {note}" if detail else note
    elif isinstance(note, str) and note:
        out["note"] = note
    return out


def resolve_member_agent_id(member_name: str) -> dict[str, Any]:
    """Resolve ``call_open``'s member name to a Grok Bot agent id.

    Uses the live directory (unix, then URL, then local profiles, then
    ``directory.json``). A snapshot entry with no id cannot be delivered.
    """
    name = (member_name or "").strip()
    book = load_directory()
    if not book.get("ok"):
        detail = str(book.get("detail") or book.get("error") or "directory unavailable")
        return _resolve_failure(book, "error", f"directory unavailable: {detail}")
    members = [m for m in (book.get("members") or []) if isinstance(m, dict)]
    if not name:
        return _resolve_failure(book, "target_not_found", "member name is empty")

    ids: list[str] = []
    found_name = False
    for member in members:
        if str(member.get("name") or "").strip() != name:
            continue
        found_name = True
        agent_id = _agent_id_of(member)
        if agent_id and agent_id not in ids:
            ids.append(agent_id)
    if len(ids) == 1:
        return {"ok": True, "id": ids[0]}
    if len(ids) > 1:
        return _resolve_failure(book, "target_not_found", f"multiple agent ids for {name}")
    if found_name:
        return _resolve_failure(
            book, "target_not_found", f"directory entry for {name} has no agent id"
        )

    for member in members:
        if _agent_id_of(member) == name:
            return {"ok": True, "id": name}
    return _resolve_failure(book, "target_not_found", f"no directory entry for {name}")


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
