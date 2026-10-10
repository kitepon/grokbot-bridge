"""通話を結び付けた会話が止まっている時に、担当フォルダの席へ返信を渡す。

席は Aiterm の session で、端末・ハーネス・フォルダごとに1つ。動いていればそこへ送り、無ければ立てる。
止まった会話に付いていた通話は、その席へ付け替える（通話の session_id は変えない）。
"""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Iterator

from . import aiterm
from .codex_delivery import DeliveryError, state_root

def seat_name(harness: str, cwd: str) -> str:
    """端末・ハーネス・フォルダごとに決まる席の名前。同じフォルダの立て直しは同じ席へ集まる。"""
    folder = re.sub(r"[^A-Za-z0-9]+", "-", Path(cwd).name).strip("-").lower()[:24] or "seat"
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(cwd)).encode("utf-8")).hexdigest()[:8]
    return f"cb-{harness.split('-')[0]}-{folder}-{digest}"


def source_key(subscription: dict[str, Any]) -> str:
    """止まった会話の名前。会話のIDを持たない結び付け（manual）は、通話ごとに1つ。"""
    conversation = subscription["thread_id"] or subscription["session_id"]
    return f"{subscription['harness']}:{conversation}"


def first_prompt(calls: list[dict[str, Any]], texts: list[str], reason: str) -> str:
    """席が最初に受け取る文。渡していない返信の本文と、引き継いだ通話の番号だけを書く。"""
    lines = [
        "通話の返信が届いています。前の会話は返信を受け取れない状態だったので、このフォルダの席へ渡しました。",
        f"理由: {reason}",
        "引き継いだ通話:",
        *[f"- session_id={call['session_id']} 相手={call['member_name']}" for call in calls],
        "前のやりとりは call-bridge の call_history（session_id）で両方の発言を読めます。",
        "返信は call_send（session_id、from_party=local）で、同じ通話へ送ってください。",
        "",
        *texts,
    ]
    return "\n".join(lines)


@contextmanager
def seat_lock(name: str) -> Iterator[None]:
    """同じ席を2つの見張りが同時に立てないための鍵。取れない時は待たずに RELAUNCH_BUSY。"""
    fd = os.open(state_root() / f"seat-{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DeliveryError("RELAUNCH_BUSY", "ほかの見張りが同じ席を立てている途中です") from exc
        yield
    finally:
        os.close(fd)


async def hand_over(store: Any, subscription: dict[str, Any], texts: list[str], reason: str,
                    connect: Callable[[], AsyncContextManager[Any]] | None = None) -> str:
    """止まった会話の通話を、担当フォルダの席へ渡す。席の名前を返す。

    RELAUNCH_BUSY は、何もしていない（後でもう一度試せる）。
    文が席へ入ったか確かめられない失敗は outcome_unknown。その時は立て直さず、送り直さない。
    """
    connect = connect or aiterm.connect
    harness, cwd = subscription["harness"], subscription.get("cwd")
    if harness not in aiterm.HARNESSES:
        raise DeliveryError("RELAUNCH_HARNESS_UNSUPPORTED", f"{harness} の席は立てられません")
    if not cwd or not Path(cwd).is_dir():
        raise DeliveryError("RELAUNCH_FOLDER_UNKNOWN", "止まった会話のフォルダが分かりません")
    source, name = source_key(subscription), seat_name(harness, cwd)
    calls = store.bound_to(harness, subscription["thread_id"], subscription["session_id"])
    text = first_prompt(calls, texts, reason)
    with seat_lock(name):
        store.note_relaunch(source, name, "starting", reason)
        try:
            async with connect() as terminal:
                seats = await terminal.sessions()
                if name in seats:
                    if seats[name] != aiterm.HARNESSES[harness]:
                        raise DeliveryError("RELAUNCH_NAME_TAKEN", f"{name} は別の端末が使っています")
                    await terminal.send(name, text)
                else:
                    seat, delivered = await terminal.launch(harness, cwd, name, text)
                    if seat != name:
                        raise DeliveryError("RELAUNCH_RECEIPT_INVALID", "立てた席の名前が違います", outcome_unknown=True)
                    if not delivered:
                        await terminal.send(name, text)
        except aiterm.AitermError as exc:
            # 起動の画面で止まった席は端末だけが残る。閉じて、次の返信がもう一度立てられるようにする。
            leftover = exc.launch.get("session_id") if exc.launch else None
            if exc.sent is False and isinstance(leftover, str) and leftover:
                try:
                    async with connect() as terminal:
                        await terminal.close(leftover)
                except Exception:  # noqa: BLE001 - 閉じられなくても、元の失敗をそのまま返す
                    pass
            store.note_relaunch(source, name, "failed" if exc.sent is False else "unknown", str(exc))
            raise
        except DeliveryError as exc:
            store.note_relaunch(source, name, "unknown" if exc.outcome_unknown else "failed", str(exc))
            raise
        store.note_relaunch(source, name, "started", reason)
        store.rebind(harness, subscription["thread_id"], subscription["session_id"], name, cwd)
    return name
