#!/usr/bin/env python3
"""On-demand phone directory for the host that has the live seat profiles.

Run this on the Grok Bot box (where ``/home/box/agent-data/agents/*/profile.json``
lives). Each ``GET /v0/directory`` rebuilds the book from those files.
Prod binds ``127.0.0.1:18765``. main-server forwards that port with
``ssh -R 127.0.0.1:18765`` and a host socat unix socket mounted into
call-bridge as ``/run/dirlive.sock`` (``CALL_BRIDGE_DIRECTORY_UNIX``).
``CALL_BRIDGE_DIRECTORY_URL`` is only the HTTP fallback.
No cron, no Marian sync, and no push after a role edit.

Env:
  CALL_BRIDGE_AGENTS_ROOT     profile tree (else ``/home/box/agent-data/agents``)
  CALL_BRIDGE_DIRECTORY_BIND  default ``127.0.0.1``
  CALL_BRIDGE_DIRECTORY_PORT  default ``18765``
  CALL_BRIDGE_DIRECTORY_TOKEN optional bearer required on ``/v0/directory``
"""

from __future__ import annotations

import hmac
import json
import os
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from call_bridge.directory import agents_root, build_directory_doc  # noqa: E402


def _expected_token() -> str:
    raw = os.environ.get("CALL_BRIDGE_DIRECTORY_TOKEN", "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def _presented_token(header: str) -> str:
    value = header.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


class DirectoryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._json(200, {"ok": True, "service": "directory-live"})
            return
        if path != "/v0/directory":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        expected = _expected_token()
        if expected:
            presented = _presented_token(self.headers.get("Authorization", ""))
            if not hmac.compare_digest(presented, expected):
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
        root = agents_root()
        if root is None:
            self._json(
                503,
                {"ok": False, "error": "agents_root_missing", "members": []},
            )
            return
        doc = build_directory_doc(root)
        doc["ok"] = True
        doc["count"] = len(doc["members"])
        self._json(200, doc)

    def _json(self, code: int, payload: dict) -> None:
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s\n" % (fmt % args))


class DirectoryHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # 起動時の逆引きDNSを待たず、bindしたアドレスをそのまま公開する。
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main() -> int:
    host = os.environ.get("CALL_BRIDGE_DIRECTORY_BIND", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("CALL_BRIDGE_DIRECTORY_PORT", "18765"))
    httpd = DirectoryHTTPServer((host, port), DirectoryHandler)
    root = agents_root()
    print(
        json.dumps(
            {
                "ok": True,
                "listen": f"http://{host}:{port}/v0/directory",
                "agents_root": str(root) if root else None,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if root is None:
        print(
            "warning: agents root not found; GET /v0/directory returns 503 until it exists",
            file=sys.stderr,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
