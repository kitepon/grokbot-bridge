"""A token command keeps the bridge token in memory; env and auth.json stay the default. A 401 says so."""
import json
import os
import shlex
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import httpx
import pytest

from call_bridge import client_token, harness_setup, local
from call_bridge.codex_delivery import DeliveryError


def _command(tmp_path: Path, value: str = "from-command", code: int = 0) -> str:
    """A command that prints `value` and counts its runs in tmp_path/runs."""
    script = tmp_path / "token_cmd.py"
    script.write_text(
        "import pathlib,sys\n"
        f"p=pathlib.Path({str(tmp_path / 'runs')!r}); p.write_text(str(int(p.read_text() or 0)+1) if p.exists() else '1')\n"
        f"print({value!r}); sys.exit({code})\n", encoding="utf-8")
    return " ".join(shlex.quote(part) for part in (sys.executable, str(script)))


def _runs(tmp_path: Path) -> int:
    file = tmp_path / "runs"
    return int(file.read_text()) if file.exists() else 0


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("CALL_BRIDGE_STATE", str(root))
    monkeypatch.delenv("CALL_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("CALL_BRIDGE_TOKEN_COMMAND", raising=False)
    client_token.forget_token()
    yield root
    client_token.forget_token()


def _write(root: Path, name: str, value: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(value), encoding="utf-8")


def test_command_token_is_resolved_once_in_memory_and_auth_json_is_not_read(state, tmp_path):
    _write(state, "config.json", {"enabled": True, "token_command": _command(tmp_path)})
    _write(state, "auth.json", {"token": "stale-plaintext"})
    assert local._headers() == {"Authorization": "Bearer from-command"}
    assert local._headers() == {"Authorization": "Bearer from-command"}
    assert _runs(tmp_path) == 1
    assert client_token.forget_token() is True
    local._headers()
    assert _runs(tmp_path) == 2


def test_without_a_command_env_then_auth_json_stay_the_default(state, monkeypatch):
    _write(state, "config.json", {"enabled": True})
    _write(state, "auth.json", {"token": "file-token"})
    assert local._headers() == {"Authorization": "Bearer file-token"}
    monkeypatch.setenv("CALL_BRIDGE_TOKEN", "env-token")
    assert local._headers() == {"Authorization": "Bearer env-token"}


def test_a_failing_command_reports_only_its_exit_code(state, tmp_path):
    _write(state, "config.json", {"enabled": True, "token_command": _command(tmp_path, "leaked-value", 3)})
    with pytest.raises(DeliveryError) as caught:
        local._headers()
    assert caught.value.code == "BRIDGE_TOKEN_COMMAND_FAILED"
    assert "exit 3" in str(caught.value) and "leaked-value" not in str(caught.value)


def test_require_token_with_a_command_records_it_and_writes_no_auth_json(state, tmp_path, monkeypatch):
    command = _command(tmp_path)
    monkeypatch.setenv("CALL_BRIDGE_TOKEN_COMMAND", command)
    harness_setup.require_token()
    config = json.loads((state / "config.json").read_text(encoding="utf-8"))
    assert config["token_command"] == command and config["enabled"] is True
    assert not (state / "auth.json").exists()
    assert "from-command" not in (state / "config.json").read_text(encoding="utf-8")


class _Unauthorized(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_POST(self):
        _Unauthorized.seen.append(self.headers.get("Authorization", ""))
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def unauthorized_bridge(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Unauthorized)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("CALL_BRIDGE_MCP_URL", f"http://127.0.0.1:{server.server_port}/mcp")
    # Some earlier watcher tests leave httpx.AsyncClient replaced by their fake bridge; use the real class.
    monkeypatch.setattr(httpx, "AsyncClient", httpx._client.AsyncClient)
    _Unauthorized.seen = []
    yield
    server.shutdown()


@pytest.mark.asyncio
async def test_401_is_an_unauthorized_error_not_a_taskgroup(state, unauthorized_bridge, monkeypatch):
    monkeypatch.setenv("CALL_BRIDGE_TOKEN", "env-token")
    with pytest.raises(DeliveryError) as caught:
        await local._remote_tool("call_directory", {"query": None})
    assert caught.value.code == "BRIDGE_UNAUTHORIZED"
    assert len(_Unauthorized.seen) == 1


@pytest.mark.asyncio
async def test_401_reruns_the_token_command_and_retries_once(state, tmp_path, unauthorized_bridge):
    _write(state, "config.json", {"enabled": True, "token_command": _command(tmp_path)})
    with pytest.raises(DeliveryError) as caught:
        await local._remote_tool("call_directory", {"query": None})
    assert caught.value.code == "BRIDGE_UNAUTHORIZED"
    assert _runs(tmp_path) == 2
    assert _Unauthorized.seen == ["Bearer from-command"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("statuses, end", [((401, 401), "failed"), ((401, 200), "active")])
async def test_watcher_reresolves_the_token_once_after_a_401(tmp_path, statuses, end):
    sid = "3f0c9a52-5b8e-4f61-9d7e-1f2a3b4c5d6e"
    store = local.LocalStore(tmp_path)
    store.add(sid, "", Path("."), "サクラ", delivery_mode="manual", harness="cursor")
    client = mock.AsyncMock()
    client.__aenter__.return_value = client
    replies = iter(statuses)

    async def get(url, **_kwargs):
        status = next(replies, None)
        if status is None:
            raise StopAsyncIteration
        body = {"ok": True, "status": "open", "messages": []} if status == 200 else {}
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))
    client.get.side_effect = get
    with mock.patch.object(local.httpx, "AsyncClient", return_value=client), \
         mock.patch.object(local, "_headers", return_value={}), \
         mock.patch.object(local.client_token, "forget_token") as forget, \
         mock.patch.object(local.asyncio, "sleep", mock.AsyncMock()):
        try:
            await local.Watchers(store)._watch_claimed(sid)
        except StopAsyncIteration:
            pass
    assert store.subscription(sid)["state"] == end
    assert forget.call_count == 1
