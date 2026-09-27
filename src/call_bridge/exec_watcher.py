"""Receive replies independently of a short-lived Codex exec process."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

from .local import _claim_session, store, watchers


async def _run(session_id: str) -> None:
    if store.subscription(session_id)["state"] != "active":
        return
    fd = _claim_session(session_id)
    if fd is None:
        return
    try:
        await watchers._watch_claimed(session_id)
    finally:
        os.close(fd)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("session_id required")
    uuid.UUID(sys.argv[1])
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(_run(sys.argv[1]))


if __name__ == "__main__":
    main()
