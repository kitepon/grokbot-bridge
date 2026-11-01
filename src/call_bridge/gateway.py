"""Talk to the Grok Bot gateway on the box loopback.

``gateway.json`` holds ``port`` and ``token``. This module reads that file
and POSTs ``/api/sendPrompt``. It never logs the token or the prompt, and a
config error does not include the file body (the body can contain the token).

The courier always dials ``127.0.0.1``. The bridge runs outside the box and
cannot reach that address; only a process on the box can.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("call_bridge.gateway")

SEND_PROMPT_TIMEOUT_SECONDS = 20
_MAX_RESPONSE_BYTES = 65536


class GatewayConfigError(Exception):
    """gateway.json cannot be used. The message never includes the token."""


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):  # noqa: ARG002
        return response

    https_response = http_response


def gateway_json_path() -> Path:
    raw = os.environ.get("CALL_BRIDGE_GATEWAY_JSON", "").strip()
    if raw:
        return Path(raw)
    home = Path.home() / "agent-data" / "gateway.json"
    box = Path("/home/box/agent-data/gateway.json")
    if home.is_file() or not box.is_file():
        return home
    return box


def load_gateway(path: Path | None = None) -> dict[str, Any]:
    """Return ``{"base_url", "token"}``. Raises GatewayConfigError without secrets."""
    path = path or gateway_json_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GatewayConfigError(f"gateway.json cannot be read ({exc.errno or 'os'})") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise GatewayConfigError("gateway.json is not valid JSON") from None
    if not isinstance(data, dict):
        raise GatewayConfigError("gateway.json must be an object")
    port = data.get("port")
    if isinstance(port, str) and port.strip().isdigit():
        port = int(port.strip())
    token = data.get("token")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise GatewayConfigError("gateway.json needs a port")
    if not isinstance(token, str) or not token.strip():
        raise GatewayConfigError("gateway.json needs a token")
    return {"base_url": f"http://127.0.0.1:{port}", "token": token.strip()}


def _scrub(text: str, token: str) -> str:
    if token and token in text:
        text = text.replace(token, "***")
    return text


def _short(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def send_prompt(
    base_url: str,
    token: str,
    agent_id: str,
    nonce: str,
    prompt: str,
    timeout: float = SEND_PROMPT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """POST sendPrompt. Never raises.

    ``outcome`` is ``accepted`` (HTTP 2xx), ``rejected`` (the gateway
    definitely did not accept it), or ``uncertain`` (it may have accepted).
    The caller must not send the same prompt again after ``uncertain``.
    ``detail`` is a short status and does not include the token or the prompt.
    """
    body = {
        "agentId": agent_id,
        "clientNonce": nonce,
        "prompt": prompt,
        "automationWriteProvenance": "untrusted",
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = base_url.rstrip("/") + "/api/sendPrompt"
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "grokbot-bridge-courier",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            code = int(getattr(response, "status", 0) or response.getcode())
            response.read(_MAX_RESPONSE_BYTES)
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        errno = getattr(reason, "errno", None)
        refused = isinstance(reason, ConnectionRefusedError) or errno in (111, 61)
        timed_out = isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError)
        if refused:
            outcome, detail = "rejected", "connection refused"
        elif timed_out:
            outcome, detail = "uncertain", "timed out"
        else:
            outcome, detail = "uncertain", "request failed"
        log.error(
            "sendPrompt %s agent_id=%s nonce=%s",
            detail,
            agent_id,
            nonce,
        )
        scrubbed = _scrub(_short(f"{type(exc).__name__}: {exc}"), token)
        log.debug("sendPrompt transport: %s", scrubbed)
        return {"outcome": outcome, "detail": detail}

    detail = f"http {code}"
    if 200 <= code < 300:
        outcome = "accepted"
        log.info("sendPrompt accepted agent_id=%s nonce=%s %s", agent_id, nonce, detail)
    elif 400 <= code < 500 and code not in (408, 429):
        # A definite 4xx means this prompt was not accepted. Redirects, 408,
        # 429, and 5xx may have been processed, so those stay uncertain.
        outcome = "rejected"
        log.error("sendPrompt rejected agent_id=%s nonce=%s %s", agent_id, nonce, detail)
    else:
        outcome = "uncertain"
        log.error("sendPrompt uncertain agent_id=%s nonce=%s %s", agent_id, nonce, detail)
    return {"outcome": outcome, "detail": detail}
