"""Who a local call_send is allowed to say it is from.

A message delivered with the box gateway's sendPrompt shows up under the
owner's name. The bridge therefore puts a fixed preface on every local
message, and it takes the team from the connection when the token is bound
to one. A bound caller cannot leave the team blank or substitute another.
When the token also names a caller id, GrokBot and BellTeam take the
display name from the phone book. A local token has no phone-book entry;
its registered token name is the display name. The request cannot
substitute a different one.
"""

from __future__ import annotations

from typing import Any

from .auth import Principal

# The sentence the recipient sees. The team word is the label below, not a
# free-form system id, so the line stays 「〇〇チームの〇〇」.
PREFACE = "これはオーナー本人ではなく、{team}チームの{name}からのメッセージです。"

TEAM_BY_SYSTEM = {
    "grokbot": "GrokBot",
    "bellteam": "BellTeam",
    "local": "ローカル",
}

_MAX_LABEL = 80


def with_sender_preface(message: str, team: str, name: str) -> str:
    """Put the fixed sender line in front of a local message. Once."""
    return f"{PREFACE.format(team=team, name=name)}\n{message}"


def canonical_team(value: str) -> str | None:
    """Map a system id or the public label to the label. Unknown text is None."""
    text = value.strip()
    if text in TEAM_BY_SYSTEM:
        return TEAM_BY_SYSTEM[text]
    for label in TEAM_BY_SYSTEM.values():
        if text == label:
            return label
    return None


def _label(value: str, field: str) -> str | dict[str, str]:
    if not value:
        return {"error": "rejected", "detail": f"{field} is required"}
    if len(value) > _MAX_LABEL or any(ch in value for ch in "\r\n\x00"):
        return {"error": "rejected", "detail": f"{field} is not a usable label"}
    return value


def _registered_sender_name(principal: Principal) -> str | dict[str, str]:
    """The only display name this id-bearing connection may use.

    Local seats are not in the GrokBot or BellTeam phone book.
    ``member_display_name`` returns None for them, so the name registered
    on the token is the fixed display name. GrokBot and BellTeam still
    have to match a directory entry.
    """
    if principal.system == "local":
        registered = principal.name.strip()
        if not registered:
            return {"error": "rejected", "detail": "this connection has no registered name"}
        return registered
    from .directory import member_display_name

    looked = member_display_name(principal.system or "", principal.id or "")
    if not looked:
        return {
            "error": "rejected",
            "detail": "this connection's name is not in the directory",
        }
    return looked


def resolve_sender(
    principal: Principal,
    sender_name: str | None,
    sender_team: str | None,
) -> dict[str, Any]:
    """Return ``sender_name`` and ``sender_team``, or an error dict.

    Unrestricted connections (dev, legacy token, ops) must supply both; the
    bridge cannot tell who they are. A token bound to a system always uses
    that system's team. A GrokBot or BellTeam token that carries a caller id
    uses the directory name for that id. A local token that carries a caller
    id uses the name registered on the token (``Principal.name``). Either
    way the caller cannot supply a different name.
    """
    if sender_name is not None and not isinstance(sender_name, str):
        return {"error": "rejected", "detail": "sender_name must be a string"}
    if sender_team is not None and not isinstance(sender_team, str):
        return {"error": "rejected", "detail": "sender_team must be a string"}
    name_in = (sender_name or "").strip()
    team_in = (sender_team or "").strip()

    if principal.unrestricted:
        if not name_in or not team_in:
            return {"error": "rejected", "detail": "sender_name and sender_team are required"}
        known = canonical_team(team_in)
        name = _label(name_in, "sender_name")
        team = _label(known or team_in, "sender_team")
        if isinstance(name, dict):
            return name
        if isinstance(team, dict):
            return team
        return {"sender_name": name, "sender_team": team}

    if principal.system not in TEAM_BY_SYSTEM:
        return {"error": "forbidden", "detail": "this connection has no team"}
    expected = TEAM_BY_SYSTEM[principal.system]
    if team_in and canonical_team(team_in) != expected:
        return {"error": "forbidden", "detail": "sender_team is fixed by this connection"}

    if principal.id:
        looked = _registered_sender_name(principal)
        if isinstance(looked, dict):
            return looked
        if name_in and name_in != looked:
            return {"error": "forbidden", "detail": "sender_name is fixed by this connection"}
        name = looked
    else:
        if not name_in:
            return {"error": "rejected", "detail": "sender_name is required"}
        name = name_in
    checked = _label(name, "sender_name")
    if isinstance(checked, dict):
        return checked
    return {"sender_name": checked, "sender_team": expected}
