"""On-demand phone directory: URL, local profiles, then directory.json."""

from __future__ import annotations

import json
import os
import sys
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from call_bridge.directory import (  # noqa: E402
    DIRECTORY_HOP_HEADER,
    DIRECTORY_URL_TIMEOUT_SECONDS,
    agents_root,
    build_members_from_profiles,
    load_directory,
    search_directory,
)

_ENV_KEYS = (
    "CALL_BRIDGE_DIRECTORY_URL",
    "CALL_BRIDGE_DIRECTORY_URL_AUTH",
    "CALL_BRIDGE_DIRECTORY_URL_TIMEOUT",
    "CALL_BRIDGE_AGENTS_ROOT",
    "CALL_BRIDGE_DIRECTORY",
    "CALL_BRIDGE_DIRECTORY_TOKEN",
    "CALL_BRIDGE_DIRECTORY_BIND",
    "CALL_BRIDGE_DIRECTORY_PORT",
)

_MISSING_AGENTS = "/nonexistent/call-bridge-agents-root"
_MISSING_SNAPSHOT = "/nonexistent/call-bridge-directory.json"


def _write_profile(
    root: Path,
    seat: str,
    name: str,
    title: str | None = None,
    description: str | None = None,
    raw: str | None = None,
) -> None:
    seat_dir = root / seat
    seat_dir.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (seat_dir / "profile.json").write_text(raw, encoding="utf-8")
        return
    payload: dict[str, str] = {"name": name}
    if title is not None:
        payload["title"] = title
    if description is not None:
        payload["description"] = description
    (seat_dir / "profile.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


class _PayloadHandler(BaseHTTPRequestHandler):
    payload: bytes = b"{}"
    status_code: int = 200
    location: str | None = None
    delay: float = 0

    def do_GET(self) -> None:  # noqa: N802
        if self.delay:
            time.sleep(self.delay)
        self.server.captured = {  # type: ignore[attr-defined]
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "hop": self.headers.get(DIRECTORY_HOP_HEADER),
        }
        body = self.payload
        self.send_response(self.status_code)
        if self.location:
            self.send_header("Location", self.location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def log_message(self, fmt: str, *args: object) -> None:
        return


class DirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = _MISSING_AGENTS
        os.environ["CALL_BRIDGE_DIRECTORY"] = _MISSING_SNAPSHOT

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_timeout_default_is_short(self) -> None:
        self.assertGreaterEqual(DIRECTORY_URL_TIMEOUT_SECONDS, 1)
        self.assertLessEqual(DIRECTORY_URL_TIMEOUT_SECONDS, 3)

    def test_local_profiles_map_fields_and_skip_placeholders(self) -> None:
        root = self._agents()
        _write_profile(root, "b-rapi", "ラピ", "インフラ統括", "回線を\n見る")
        _write_profile(root, "a-marian", "マリアン", "電話番", "起こすだけ")
        _write_profile(root, "new", "New Agent", "x", "y")
        _write_profile(root, "guest", "ゲスト", "x", "y")
        _write_profile(root, "blank", "  ", "x", "y")
        _write_profile(root, "bad", "ignored", raw="{")
        (root / "notes.txt").write_text("nope", encoding="utf-8")
        (root / "empty-seat").mkdir()

        members = build_members_from_profiles(root)

        self.assertEqual(
            members,
            [
                {"name": "マリアン", "title": "電話番", "role": "起こすだけ"},
                {"name": "ラピ", "title": "インフラ統括", "role": "回線を 見る"},
            ],
        )

    def test_local_profile_edit_is_visible_on_the_next_read(self) -> None:
        root = self._agents()
        _write_profile(root, "rapi", "ラピ", "インフラ統括", "旧")
        first = load_directory()
        self.assertEqual(first["source"], "agent-profiles")
        self.assertEqual(first["agents_root"], str(root))
        self.assertEqual(first["members"][0]["title"], "インフラ統括")
        self.assertNotIn("directory_url", first)

        _write_profile(root, "rapi", "ラピ", "インフラ統括・改", "新しい役割")
        second = search_directory("ラピ")
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["members"][0]["title"], "インフラ統括・改")
        self.assertEqual(second["members"][0]["role"], "新しい役割")

    def test_url_is_preferred_and_reports_live_source(self) -> None:
        root = self._agents()
        _write_profile(root, "local", "ローカル席", "古い肩書き", "ローカル")
        self._write_snapshot([{"name": "スナップ", "title": "ファイル", "role": "予備"}])
        server = self._serve(
            {
                "ok": True,
                "schema": "grokbot.directory.v0",
                "source": "agent-profiles",
                "agents_root": "/home/box/agent-data/agents",
                "members": [
                    {"name": "ラピ", "title": "新しい肩書き", "role": "ライブ"},
                    {"name": "マリアン", "title": "電話番", "role": "起こす"},
                ],
            }
        )
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_DIRECTORY_URL_AUTH"] = "live-token"

        book = search_directory("肩書き")

        self.assertTrue(book["ok"])
        self.assertEqual(book["source"], "agent-profiles")
        self.assertEqual(book["agents_root"], "/home/box/agent-data/agents")
        self.assertEqual(book["directory_url"], self._url(server))
        self.assertEqual(book["count"], 1)
        self.assertEqual(book["members"][0]["name"], "ラピ")
        self.assertEqual(book["members"][0]["title"], "新しい肩書き")
        captured = server.captured  # type: ignore[attr-defined]
        self.assertEqual(captured["authorization"], "Bearer live-token")
        self.assertEqual(captured["hop"], "1")
        self.assertTrue(captured["path"].startswith("/v0/directory"))

    def test_auth_header_keeps_existing_bearer_scheme(self) -> None:
        server = self._serve(
            {
                "source": "agent-profiles",
                "agents_root": "/profiles",
                "members": [{"name": "ラピ", "title": "t", "role": "r"}],
            }
        )
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_DIRECTORY_URL_AUTH"] = "Bearer already"

        book = load_directory()

        self.assertEqual(book["members"][0]["name"], "ラピ")
        captured = server.captured  # type: ignore[attr-defined]
        self.assertEqual(captured["authorization"], "Bearer already")

    def test_url_failure_falls_back_to_local_profiles(self) -> None:
        root = self._agents()
        _write_profile(root, "rapi", "ラピ", "ローカル最新", "ここ")
        self._write_snapshot([{"name": "スナップ", "title": "古い", "role": "ファイル"}])
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._closed_url()

        book = load_directory()

        self.assertEqual(book["source"], "agent-profiles")
        self.assertEqual(book["agents_root"], str(root))
        self.assertEqual(book["members"][0]["title"], "ローカル最新")
        self.assertNotIn("directory_url", book)
        self.assertNotIn("directory_url_error", book)
        self.assertNotIn("path", book)

    def test_url_failure_then_snapshot_is_not_labeled_live(self) -> None:
        path = self._write_snapshot(
            [{"name": "スナップ", "title": "古い", "role": "ファイル"}],
            source="agent-profiles",
            agents_root="/home/box/agent-data/agents",
        )
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._closed_url()

        book = load_directory()

        self.assertEqual(book["source"], "directory.json")
        self.assertEqual(book["path"], str(path.resolve()))
        self.assertNotIn("agents_root", book)
        self.assertEqual(book["directory_url_error"], "request failed")
        self.assertEqual(book["members"][0]["name"], "スナップ")

    def test_repo_snapshot_is_not_reported_as_live(self) -> None:
        os.environ.pop("CALL_BRIDGE_DIRECTORY", None)
        book = load_directory()
        self.assertTrue(book["ok"])
        self.assertEqual(book["source"], "directory.json")
        self.assertNotIn("agents_root", book)
        self.assertNotIn("directory_url", book)
        self.assertGreater(book["count"], 0)

    def test_redirect_is_not_followed(self) -> None:
        bait = self._serve(
            {
                "source": "agent-profiles",
                "agents_root": "/bait",
                "members": [{"name": "餌", "title": "no", "role": "no"}],
            }
        )
        server = self._serve({}, status=302, location=self._url(bait))
        root = self._agents()
        _write_profile(root, "rapi", "ラピ", "ローカル", "ok")
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_DIRECTORY_URL_AUTH"] = "secret"

        book = load_directory()

        self.assertEqual(book["source"], "agent-profiles")
        self.assertEqual(book["agents_root"], str(root))
        self.assertEqual(book["members"][0]["name"], "ラピ")
        self.assertIsNone(bait.captured)  # type: ignore[attr-defined]

    def test_http_error_and_bad_json_fall_through(self) -> None:
        server = self._serve({}, status=502)
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(server)
        path = self._write_snapshot([{"name": "スナップ", "role": "予備"}])

        book = load_directory()
        self.assertEqual(book["source"], "directory.json")
        self.assertEqual(book["directory_url_error"], "http 502")
        self.assertEqual(book["path"], str(path.resolve()))

        server.RequestHandlerClass.status_code = 200
        server.RequestHandlerClass.payload = b"not-json"
        book = load_directory()
        self.assertEqual(book["directory_url_error"], "invalid directory json")
        self.assertEqual(book["source"], "directory.json")

    def test_ok_false_payload_falls_through(self) -> None:
        server = self._serve({"ok": False, "error": "agents_root_missing", "members": []})
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(server)
        self._write_snapshot([{"name": "スナップ", "role": "予備"}])

        book = load_directory()

        self.assertEqual(book["source"], "directory.json")
        self.assertEqual(book["directory_url_error"], "agents_root_missing")

    def test_timeout_falls_back(self) -> None:
        server = self._serve({}, delay=2)
        root = self._agents()
        _write_profile(root, "rapi", "ラピ", "間に合った", "local")
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(server)
        os.environ["CALL_BRIDGE_DIRECTORY_URL_TIMEOUT"] = "0.3"

        started = time.monotonic()
        book = load_directory()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertEqual(book["source"], "agent-profiles")
        self.assertEqual(book["members"][0]["title"], "間に合った")

    def test_skip_url_does_not_fetch(self) -> None:
        server = self._serve(
            {
                "source": "agent-profiles",
                "agents_root": "/remote",
                "members": [{"name": "リモート", "title": "遠", "role": "遠"}],
            }
        )
        root = self._agents()
        _write_profile(root, "rapi", "ラピ", "近", "近")
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._url(server)

        book = search_directory(skip_url=True)

        self.assertEqual(book["members"][0]["name"], "ラピ")
        self.assertIsNone(server.captured)  # type: ignore[attr-defined]

    def test_all_sources_missing(self) -> None:
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = self._closed_url()
        with mock.patch(
            "call_bridge.directory._snapshot_paths",
            return_value=[Path(_MISSING_SNAPSHOT)],
        ):
            book = load_directory()
        self.assertFalse(book["ok"])
        self.assertEqual(book["error"], "directory_unavailable")
        self.assertEqual(book["members"], [])
        self.assertEqual(book["directory_url_error"], "request failed")

    def test_explicit_missing_agents_root_does_not_use_default(self) -> None:
        default = Path(self._tmp()) / "box"
        default.mkdir()
        _write_profile(default, "rapi", "ラピ", "既定パス", "no")
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = _MISSING_AGENTS
        with mock.patch("call_bridge.directory.DEFAULT_AGENTS_ROOT", default):
            self.assertIsNone(agents_root())
            book = load_directory()
        self.assertEqual(book["source"], "directory.json")
        self.assertNotEqual(book["members"][0]["name"], "ラピ")

    def test_unset_agents_root_uses_default_directory(self) -> None:
        os.environ.pop("CALL_BRIDGE_AGENTS_ROOT", None)
        default = Path(self._tmp()) / "box"
        default.mkdir()
        _write_profile(default, "rapi", "ラピ", "既定パス", "box")
        with mock.patch("call_bridge.directory.DEFAULT_AGENTS_ROOT", default):
            book = load_directory()
        self.assertEqual(book["source"], "agent-profiles")
        self.assertEqual(book["agents_root"], str(default))
        self.assertEqual(book["members"][0]["title"], "既定パス")

    def test_live_server_script_sees_profile_edit_on_next_get(self) -> None:
        root = Path(self._tmp()) / "agents"
        root.mkdir()
        _write_profile(root, "rapi", "ラピ", "インフラ統括", "旧説明")
        url = self._start_live_server(root, token="box-token")
        os.environ["CALL_BRIDGE_DIRECTORY_URL"] = url
        os.environ["CALL_BRIDGE_DIRECTORY_URL_AUTH"] = "box-token"
        self._write_snapshot([{"name": "スナップ", "title": "古い", "role": "no"}])

        first = load_directory()
        self.assertEqual(first["source"], "agent-profiles")
        self.assertEqual(first["agents_root"], str(root))
        self.assertEqual(first["directory_url"], url)
        self.assertEqual(first["members"][0]["title"], "インフラ統括")
        self.assertEqual(first["members"][0]["role"], "旧説明")

        _write_profile(root, "rapi", "ラピ", "インフラ統括・改", "新しい説明")
        second = load_directory()
        self.assertEqual(second["members"][0]["title"], "インフラ統括・改")
        self.assertEqual(second["members"][0]["role"], "新しい説明")

        os.environ["CALL_BRIDGE_DIRECTORY_URL_AUTH"] = "wrong"
        denied = load_directory()
        self.assertEqual(denied["source"], "directory.json")
        self.assertEqual(denied["directory_url_error"], "http 401")
        self.assertEqual(denied["members"][0]["name"], "スナップ")
        self.assertNotIn("agents_root", denied)

    def _agents(self) -> Path:
        root = Path(self._tmp()) / "agents"
        root.mkdir()
        os.environ["CALL_BRIDGE_AGENTS_ROOT"] = str(root)
        return root

    def _tmp(self) -> str:
        path = self.id().replace(".", "_")
        import tempfile

        directory = tempfile.mkdtemp(prefix=f"{path}-")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def _write_snapshot(
        self,
        members: list[dict[str, str]],
        *,
        source: str = "agent-profiles",
        agents_root: str = "/home/box/agent-data/agents",
    ) -> Path:
        path = Path(self._tmp()) / "directory.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "grokbot.directory.v0",
                    "source": source,
                    "agents_root": agents_root,
                    "members": members,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.environ["CALL_BRIDGE_DIRECTORY"] = str(path)
        return path

    def _serve(
        self,
        payload: dict,
        *,
        status: int = 200,
        location: str | None = None,
        delay: float = 0,
    ) -> ThreadingHTTPServer:
        handler = type(
            f"Handler{id(self)}{status}{int(delay * 10)}",
            (_PayloadHandler,),
            {
                "payload": json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "status_code": status,
                "location": location,
                "delay": delay,
            },
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.captured = None  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def _url(server: ThreadingHTTPServer) -> str:
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/v0/directory"

    def _closed_url(self) -> str:
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return f"http://127.0.0.1:{port}/v0/directory"

    def _start_live_server(self, root: Path, *, token: str) -> str:
        import select
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        script = Path(__file__).resolve().parents[1] / "scripts" / "directory_live_server.py"
        env = os.environ.copy()
        env["CALL_BRIDGE_AGENTS_ROOT"] = str(root)
        env["CALL_BRIDGE_DIRECTORY_TOKEN"] = token
        env["CALL_BRIDGE_DIRECTORY_BIND"] = "127.0.0.1"
        env["CALL_BRIDGE_DIRECTORY_PORT"] = str(port)
        env.pop("CALL_BRIDGE_DIRECTORY_URL", None)
        env.pop("CALL_BRIDGE_DIRECTORY_URL_AUTH", None)
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )

        def _stop() -> None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()

        self.addCleanup(_stop)
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], 5)
        if not ready:
            err = proc.stderr.read() if proc.stderr else ""
            self.fail(f"live server did not start: {err}")
        line = proc.stdout.readline()
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            err = proc.stderr.read() if proc.stderr else ""
            self.fail(f"live server output {line!r} stderr={err}")
        self.assertTrue(info["ok"])
        self.assertEqual(info["agents_root"], str(root))
        return str(info["listen"])


if __name__ == "__main__":
    unittest.main()
