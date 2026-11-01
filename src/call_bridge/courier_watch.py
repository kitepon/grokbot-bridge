"""Wake once per courier outage with queued mail, retained across restarts."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from urllib.parse import urlsplit

from .db import CallStore
from .deliver import grokbot_delivery_mode
from .wake import _ReturnHTTPStatus, WAKE_TIMEOUT_SECONDS

log = logging.getLogger("call_bridge.courier_watch")
SILENCE_SECONDS = 300
CHECK_SECONDS = 10
URL_ENV = "CALL_BRIDGE_COURIER_WAKE_WEBHOOK_URL"
KEY_ENV = "CALL_BRIDGE_COURIER_WAKE_WEBHOOK_KEY"
COURIER_PATHS = frozenset((
    "/v0/grokbot/deliveries/inflight", "/v0/grokbot/deliveries/claim", "/v0/grokbot/directory",
))


def enabled() -> bool:
    return grokbot_delivery_mode() == "courier" and bool(
        os.environ.get(URL_ENV, "").strip() and os.environ.get(KEY_ENV, "").strip()
    )


def notify(payload: dict) -> None:
    try:
        url = os.environ[URL_ENV].strip()
        key = os.environ[KEY_ENV].strip()
        target = urlsplit(url)
        if target.scheme not in ("http", "https") or not target.netloc or "\r" in key or "\n" in key:
            raise ValueError("invalid webhook configuration")
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        opener = urllib.request.build_opener(_ReturnHTTPStatus)
        with opener.open(request, timeout=WAKE_TIMEOUT_SECONDS) as response:
            code = response.status
        log.log(logging.INFO if 200 <= code < 300 else logging.ERROR,
                "courier silence webhook attempted: http %d", code)
    except Exception:
        # URLs and exception messages can contain credentials; report neither.
        log.error("courier silence webhook failed; no retry until courier returns")


async def record_activity(store: CallStore, principal, path: str, status: int) -> None:
    if (enabled() and principal.system == "grokbot" and principal.courier
            and path in COURIER_PATHS and 200 <= status < 300):
        try:
            await asyncio.to_thread(store.record_courier_seen, time.time())
        except Exception:
            log.error("courier heartbeat could not be saved")


async def watch(store: CallStore) -> None:
    while True:
        try:
            if enabled():
                now = time.time()
                await asyncio.to_thread(store.start_courier_monitor, now)
                payload = await asyncio.to_thread(store.claim_courier_silence, now - SILENCE_SECONDS)
                if payload is not None:
                    # Claim is durable before POST: an unknown result must not cause a second wake.
                    await asyncio.to_thread(notify, payload)
        except Exception:
            log.error("courier silence monitor could not check its saved state")
        await asyncio.sleep(CHECK_SECONDS)
