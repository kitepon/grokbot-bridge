"""Throughline が立てた後継の会話へ、前の会話が持っていた通話を知らせる文を作る。

Claude Code の UserPromptSubmit の hook（steer/call-bridge-claude-prompt.mjs）が、後継の最初の文
（「Throughline自動継続 <引き継ぎID>」）を見た時に呼ぶ。前の会話の通話があれば、知らせる文を stdout へ出す。無ければ何も出さない。

通話の届け先は、ここでは替えない。受け口は、会話が自分で通話の道具（call_adopt）を呼んだ時に開く。
ここは控えを読むだけで、何も書かない。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from typing import Any

from . import claude_channel
from . import conversation
from .codex_delivery import DeliveryError, state_root


def _calls() -> list[dict[str, Any]]:
    """この端末で開いた、Claude Code の会話の受け口へ届けている通話（生きている物と、配送を止めてある物）。"""
    path = state_root() / "local.sqlite"
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT session_id, member_name, thread_id FROM subscriptions "
                              "WHERE state IN ('active', 'yielding', 'paused') AND delivery_mode = 'channel' "
                              "AND harness = 'claude-code' ORDER BY rowid").fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


async def inherited(handoff_id: str) -> list[dict[str, Any]]:
    """引き継ぎの、前の会話が持っていた通話。Throughline が無い・記録が無い・読めない時は空。"""
    record = await conversation.claude_handoff(handoff_id)
    source = record.get("source_session_id") if record else None
    if not isinstance(source, str) or not source:
        return []
    calls = _calls()
    try:
        owners = await claude_channel.owners(sorted({call["thread_id"] for call in calls}))
    except DeliveryError:
        return []
    return [call for call in calls if owners.get(call["thread_id"]) == source]


def hint(calls: list[dict[str, Any]]) -> str:
    if not calls:
        return ""
    lines = [
        "通話MCP（call-bridge）からの知らせです。この会話は、Throughline が引き継いだ会話の続きです。",
        "前の会話は、次の通話を持っていました。返信をこの会話で受け取るために、作業を続ける前に、"
        "call-bridge の call_adopt を通話ごとに1回ずつ呼んでください（引数は session_id だけ）。",
        *[f"- session_id={call['session_id']} 相手={call['member_name']}" for call in calls],
        "引き取った後は、相手の返信がこの会話へ自動で届きます。前の会話あてに届いたまま読まれていない返信も、ここへ渡ります。",
    ]
    return "\n".join(lines)


def main() -> None:
    try:
        text = hint(asyncio.run(inherited(sys.argv[1]))) if len(sys.argv) > 1 else ""
    except Exception:  # noqa: BLE001 - hook の中。失敗で会話の文を止めない
        text = ""
    if text:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
