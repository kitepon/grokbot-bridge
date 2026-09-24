"""Phone directory from Grok Bot agent profiles (name / title / description as-is).

No call-permission flags. Source of truth is each seat's profile settings.
When CALL_BRIDGE_AGENTS_ROOT (or the default box path) is readable, entries are
built live on every request. Otherwise falls back to directory.json (kept in
sync from the same builder).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCHEMA = "grokbot.directory.v0"

# Placeholders / retired seats — not a phone book.
_SKIP_NAMES = {"", "New Agent", "New Bot", "ゲスト"}


def agents_root() -> Path | None:
    env = os.environ.get("CALL_BRIDGE_AGENTS_ROOT", "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path("/home/box/agent-data/agents"))
    for p in candidates:
        if p.is_dir():
            return p
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


def load_directory() -> dict[str, Any]:
    root = agents_root()
    if root is not None:
        members = build_members_from_profiles(root)
        return {
            "ok": True,
            "schema": SCHEMA,
            "source": "agent-profiles",
            "agents_root": str(root),
            "count": len(members),
            "members": members,
        }

    for path in _snapshot_paths():
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            members = data.get("members") or []
            return {
                "ok": True,
                "path": str(path.resolve()),
                "schema": data.get("schema") or SCHEMA,
                "source": data.get("source") or "directory.json",
                "count": len(members),
                "members": members,
            }
    return {
        "ok": False,
        "error": "directory_unavailable",
        "detail": (
            "No agent profiles and no directory.json. "
            "Set CALL_BRIDGE_AGENTS_ROOT or sync directory.json from profiles."
        ),
        "count": 0,
        "members": [],
    }


def search_directory(query: str | None = None) -> dict[str, Any]:
    base = load_directory()
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
