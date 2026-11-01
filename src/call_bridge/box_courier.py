"""Box-side courier: pull GrokBot messages and deliver them with sendPrompt.

The bridge (call.kitepon.dev) is outside the box, so it cannot call
``127.0.0.1``. This process runs on the box, connects outward, claims each
new message once, POSTs ``/api/sendPrompt``, and reports the outcome with
``call_receipt``.

A journal entry is fsynced before sendPrompt:

* no entry, or ``held`` — sendPrompt was not called; send once
* ``armed`` or ``uncertain`` — the call may have reached the gateway;
  report ``unknown`` and do not send again
* ``accepted`` — report ``submitted``
* ``rejected`` — report ``failed`` so a definite refusal can be retried
  with the same clientNonce

A second process exits instead of claiming the same messages.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .directory import agents_root, build_directory_doc
from .gateway import GatewayConfigError, load_gateway, send_prompt

log = logging.getLogger("call_bridge.box_courier")

DEFAULT_BRIDGE_URL = "https://call.kitepon.dev"
DEFAULT_INTERVAL_SECONDS = 2.0
_MAX_BODY = 1_000_000


class CourierError(Exception):
    """The courier cannot continue this pass. The message has no secrets."""


class BridgeError(CourierError):
    """The bridge did not accept a courier request."""


class CourierBusy(CourierError):
    """Another courier process holds the lock."""


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):  # noqa: ARG002
        return response

    https_response = http_response


def restart_action(phase: str | None) -> str:
    """What to do with an inflight claim given the local journal phase."""
    if phase in (None, "held"):
        return "send"
    if phase == "accepted":
        return "submitted"
    if phase == "rejected":
        return "failed"
    return "unknown"


def state_dir() -> Path:
    raw = os.environ.get("CALL_BRIDGE_COURIER_STATE", "").strip()
    if raw:
        return Path(raw)
    box = Path("/home/box/agent-data")
    if box.is_dir():
        return box / "call-bridge-courier"
    return Path.home() / ".call-bridge-courier"


def bridge_token() -> str:
    """Token for the bridge. A file wins over the environment, so `ps` need not show it."""
    file_name = os.environ.get("CALL_BRIDGE_TOKEN_FILE", "").strip()
    if file_name:
        try:
            text = Path(file_name).read_text(encoding="utf-8")
        except OSError as exc:
            raise CourierError(f"CALL_BRIDGE_TOKEN_FILE cannot be read ({exc.errno or 'os'})") from None
        line = text.strip().splitlines()[0].strip() if text.strip() else ""
        if not line:
            raise CourierError("CALL_BRIDGE_TOKEN_FILE is empty")
        return line
    token = os.environ.get("CALL_BRIDGE_TOKEN", "").strip()
    if not token:
        raise CourierError("set CALL_BRIDGE_TOKEN_FILE or CALL_BRIDGE_TOKEN")
    return token


def _interval() -> float:
    raw = os.environ.get("CALL_BRIDGE_COURIER_INTERVAL", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS
    if value < 0.2:
        return DEFAULT_INTERVAL_SECONDS
    return value


def _courier_id() -> str:
    raw = os.environ.get("CALL_BRIDGE_COURIER_ID", "box").strip() or "box"
    if len(raw) > 80 or any(ch in raw for ch in "\r\n\x00"):
        raise CourierError("CALL_BRIDGE_COURIER_ID is not usable")
    return raw


class CourierLock:
    """One courier process per state directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise CourierBusy("another courier is already running") from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None


class Journal:
    """Fsynced record of how far sendPrompt got for each claimed message."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._read()

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise CourierError("courier journal cannot be read; not sending") from None
        if not isinstance(data, dict):
            raise CourierError("courier journal cannot be read; not sending")
        return data

    @staticmethod
    def key(session_id: str, seq: int) -> str:
        return f"{session_id}:{seq}"

    def phase(self, session_id: str, seq: int) -> str | None:
        row = self.data.get(self.key(session_id, seq))
        if not isinstance(row, dict):
            return None
        phase = row.get("phase")
        return phase if isinstance(phase, str) else None

    def put(self, session_id: str, seq: int, phase: str, **fields: object) -> None:
        self.data[self.key(session_id, seq)] = {"phase": phase, **fields}
        self._save()

    def delete(self, session_id: str, seq: int) -> None:
        key = self.key(session_id, seq)
        if key not in self.data:
            return
        del self.data[key]
        self._save()

    def discard_except(self, keys: set[str]) -> None:
        kept = {key: value for key, value in self.data.items() if key in keys}
        if kept.keys() == self.data.keys():
            return
        self.data = kept
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class BridgeClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _scrub(self, text: str) -> str:
        if self.token and self.token in text:
            return text.replace(self.token, "***")
        return text

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "grokbot-bridge-courier",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
            method=method,
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                code = int(getattr(response, "status", 0) or response.getcode())
                raw = response.read(_MAX_BODY)
        except urllib.error.HTTPError as exc:
            raise BridgeError(f"http {exc.code}") from None
        except Exception as exc:
            detail = "timed out" if isinstance(exc, TimeoutError) else "request failed"
            log.debug("bridge %s %s", detail, self._scrub(f"{type(exc).__name__}: {exc}"))
            raise BridgeError(detail) from None
        if not 200 <= code < 300:
            raise BridgeError(f"http {code}")
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeError, json.JSONDecodeError):
            raise BridgeError("invalid json") from None
        if not isinstance(data, dict):
            raise BridgeError("invalid json")
        if data.get("ok") is False:
            raise BridgeError(str(data.get("error") or "rejected"))
        return data

    def inflight(self, courier_id: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"courier_id": courier_id})
        data = self._request("GET", f"/v0/grokbot/deliveries/inflight?{query}")
        return _delivery_list(data)

    def claim(self, courier_id: str) -> list[dict[str, Any]]:
        data = self._request(
            "POST",
            "/v0/grokbot/deliveries/claim",
            {"courier_id": courier_id, "limit": 1},
        )
        return _delivery_list(data)

    def receipt(self, item: dict[str, Any], state: str, detail: str, retryable: bool) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v0/sessions/{urllib.parse.quote(str(item['session_id']), safe='')}/receipts",
            {
                "seq": int(item["seq"]),
                "state": state,
                "detail": detail,
                "claim_token": item["claim_token"],
                "retryable": retryable,
            },
        )

    def push_directory(self, doc: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", "/v0/grokbot/directory", doc)


def _delivery_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = data.get("deliveries")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BridgeError("delivery list is invalid")
    return items


def _usable(item: dict[str, Any]) -> bool:
    prompt = item.get("prompt")
    return all(isinstance(item.get(key), str) and item.get(key) for key in ("session_id", "agent_id", "nonce", "claim_token")) and isinstance(item.get("seq"), int) and isinstance(prompt, str) and bool(prompt)


def _send_recorded(journal: Journal, gateway: dict[str, str], item: dict[str, Any]) -> dict[str, str]:
    """Write the journal, then call sendPrompt. A crash after ``armed`` is not resent."""
    session_id, seq = str(item["session_id"]), int(item["seq"])
    journal.put(session_id, seq, "held", nonce=item["nonce"], claim_token=item["claim_token"])
    journal.put(session_id, seq, "armed", nonce=item["nonce"], claim_token=item["claim_token"])
    result = send_prompt(
        gateway["base_url"],
        gateway["token"],
        str(item["agent_id"]),
        str(item["nonce"]),
        str(item["prompt"]),
    )
    phase = {"accepted": "accepted", "rejected": "rejected"}.get(result["outcome"], "uncertain")
    journal.put(
        session_id, seq, phase,
        nonce=item["nonce"], claim_token=item["claim_token"], detail=result["detail"],
    )
    return result


def _finish(
    bridge: BridgeClient,
    journal: Journal,
    item: dict[str, Any],
    state: str,
    detail: str,
    retryable: bool,
) -> tuple[bool, str]:
    """Report the outcome. The first value is whether the journal entry can be dropped.

    A failed report leaves the journal in place. ``unknown`` is still not a
    reason to send the prompt again.
    """
    try:
        result = bridge.receipt(item, state, detail, retryable)
    except BridgeError as exc:
        log.error(
            "receipt failed session_id=%s seq=%s %s",
            item.get("session_id"), item.get("seq"), exc,
        )
        return False, str(exc)
    log.info(
        "receipt session_id=%s seq=%s state=%s",
        item.get("session_id"), item.get("seq"), result.get("state"),
    )
    return result.get("state") != "inflight", ""


def deliver_item(
    bridge: BridgeClient,
    journal: Journal,
    gateway: dict[str, str],
    item: dict[str, Any],
    action: str,
) -> str:
    """``action`` is ``send``, ``submitted``, ``failed``, or ``unknown``.

    Returns a bridge error detail when the receipt did not land, else ``""``.
    An ``unknown`` outcome still does not send again, and its journal entry
    stays until the receipt is accepted.
    """
    session_id, seq = str(item.get("session_id")), item.get("seq")
    if action == "send" and not _usable(item):
        log.error("claim missing fields session_id=%s seq=%s", session_id, seq)
        if isinstance(item.get("claim_token"), str) and item.get("claim_token") and isinstance(item.get("session_id"), str) and isinstance(item.get("seq"), int):
            dropped, error = _finish(bridge, journal, item, "failed", "claim missing fields", False)
            if dropped:
                journal.delete(str(item["session_id"]), int(item["seq"]))
            return error
        return "claim missing fields"
    if action == "send":
        result = _send_recorded(journal, gateway, item)
        if result["outcome"] == "accepted":
            state, retryable = "submitted", False
        elif result["outcome"] == "rejected":
            state, retryable = "failed", True
        else:
            state, retryable = "unknown", False
        detail = result["detail"]
    else:
        state, retryable = action, action == "failed"
        detail = "not resent" if action == "unknown" else action
    dropped, error = _finish(bridge, journal, item, state, detail, retryable)
    if dropped:
        journal.delete(str(item["session_id"]), int(item["seq"]))
    return error


def pass_exit_code(stats: dict[str, Any]) -> int:
    """Exit status of one ``--once`` pass.

    ``0`` means inflight, claim, receipts, and the directory push all
    completed. ``2`` means the bridge did not complete (inflight, claim, or
    a receipt). ``3`` means only the directory push failed. ``4`` means both.
    A delivery outcome of ``unknown`` is not one of these failures.
    """
    bridge_failed = bool(stats.get("inflight_error") or stats.get("claim_error") or stats.get("receipt_error"))
    push_failed = bool(stats.get("push_error"))
    if bridge_failed and push_failed:
        return 4
    if push_failed:
        return 3
    if bridge_failed:
        return 2
    return 0


def format_pass(stats: dict[str, Any]) -> str:
    """One line that names which part of the pass failed. No secrets."""
    code = pass_exit_code(stats)
    parts = [
        "pass",
        "ok" if code == 0 else "failed",
        f"exit={code}",
        f"reconciled={stats.get('reconciled', 0)}",
        f"sent={stats.get('sent', 0)}",
        f"pushed={stats.get('pushed', 0)}",
    ]
    for key, label in (
        ("inflight_error", "inflight"),
        ("claim_error", "claim"),
        ("receipt_error", "receipt"),
        ("push_error", "directory"),
    ):
        detail = stats.get(key)
        if detail:
            parts.append(f"{label}={detail}")
    return " ".join(str(part) for part in parts)


def run_once(
    bridge: BridgeClient,
    journal: Journal,
    gateway: dict[str, str],
    courier_id: str,
) -> dict[str, Any]:
    """Reconcile inflight claims, deliver at most one new message, push the phone book.

    Bridge and directory failures are recorded on the result. They do not
    discard a journal entry, and they do not turn ``unknown`` into another send.
    """
    stats: dict[str, Any] = {
        "reconciled": 0,
        "sent": 0,
        "pushed": 0,
        "inflight_error": "",
        "claim_error": "",
        "receipt_error": "",
        "push_error": "",
    }
    inflight: list[dict[str, Any]] | None
    try:
        inflight = bridge.inflight(courier_id)
    except BridgeError as exc:
        log.error("inflight list failed: %s", exc)
        stats["inflight_error"] = str(exc)
        inflight = None
    keep: set[str] = set()
    if inflight is not None:
        for item in inflight:
            if not isinstance(item.get("seq"), int) or not isinstance(item.get("session_id"), str):
                continue
            action = restart_action(journal.phase(item["session_id"], item["seq"]))
            receipt_error = deliver_item(bridge, journal, gateway, item, action)
            if receipt_error and not stats["receipt_error"]:
                stats["receipt_error"] = receipt_error
            if action == "send":
                stats["sent"] += 1
            else:
                stats["reconciled"] += 1
            key = Journal.key(item["session_id"], item["seq"])
            if key in journal.data:
                keep.add(key)
        journal.discard_except(keep)
    try:
        claimed = bridge.claim(courier_id)
    except BridgeError as exc:
        log.error("claim failed: %s", exc)
        stats["claim_error"] = str(exc)
        claimed = []
    for item in claimed:
        receipt_error = deliver_item(bridge, journal, gateway, item, "send")
        if receipt_error and not stats["receipt_error"]:
            stats["receipt_error"] = receipt_error
        stats["sent"] += 1
    root = agents_root()
    if root is not None:
        try:
            bridge.push_directory(build_directory_doc(root))
            stats["pushed"] = 1
        except (BridgeError, OSError, ValueError) as exc:
            log.error("directory push failed: %s", exc)
            stats["push_error"] = str(exc)
    else:
        log.error("agents root not found; directory was not pushed")
        stats["push_error"] = "agents root not found"
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deliver GrokBot call-bridge messages from inside the box.")
    parser.add_argument("--once", action="store_true", help="claim, deliver, and push once, then exit")
    args = parser.parse_args(argv)
    log.setLevel(logging.INFO)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock = CourierLock(directory / "courier.lock")
    try:
        lock.acquire()
    except CourierBusy as exc:
        log.error("%s", exc)
        return 1
    try:
        try:
            gateway = load_gateway()
            token = bridge_token()
            courier_id = _courier_id()
        except (GatewayConfigError, CourierError) as exc:
            log.error("%s", exc)
            return 1
        bridge_url = os.environ.get("CALL_BRIDGE_URL", "").strip() or DEFAULT_BRIDGE_URL
        bridge = BridgeClient(bridge_url, token)
        journal = Journal(directory / "journal.json")
        interval = _interval()
        log.info("courier id=%s bridge=%s interval=%s", courier_id, bridge_url, interval)
        while True:
            try:
                stats = run_once(bridge, journal, gateway, courier_id)
                line = format_pass(stats)
                code = pass_exit_code(stats)
                if code == 0:
                    log.info("%s", line)
                else:
                    log.error("%s", line)
            except CourierError as exc:
                log.error("%s", exc)
                if args.once:
                    return 1
                time.sleep(interval)
                continue
            except Exception:
                log.error("courier pass failed")
                if args.once:
                    return 1
                time.sleep(interval)
                continue
            if args.once:
                return code
            time.sleep(interval)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
