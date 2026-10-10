"""常駐の受け取り係の登録の中身と、Claude Code・Cursor・Grok への登録。端末へは何も登録しない。"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from call_bridge import harness_setup, local, receiver
from call_bridge.codex_delivery import DeliveryError


class ReceiverPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.tmp.name, "HOME": self.tmp.name,
                                      "PATH": "/opt/homebrew/bin:/usr/bin", "XDG_CONFIG_HOME": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_macos_agent_runs_in_the_screen_session_and_keeps_the_install_time_path(self) -> None:
        with patch.object(Path, "home", return_value=Path(self.tmp.name)):
            file, body = receiver.launch_agent()
        plan = plistlib.loads(body)
        self.assertEqual(file, Path(self.tmp.name) / "Library/LaunchAgents/dev.kitepon.call-bridge.receiver.plist")
        self.assertEqual(plan["ProgramArguments"], [sys.executable, "-m", "call_bridge.receiver"])
        self.assertEqual((plan["LimitLoadToSessionType"], plan["RunAtLoad"], plan["KeepAlive"]), ("Aqua", True, True))
        self.assertEqual(plan["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin")
        self.assertEqual(plan["EnvironmentVariables"]["CALL_BRIDGE_STATE"], self.tmp.name)

    def test_linux_unit_quotes_paths_and_starts_with_the_user_session(self) -> None:
        with patch.object(Path, "home", return_value=Path(self.tmp.name)), \
             patch.object(receiver.sys, "executable", "/home/k ite/py%thon"):
            file, body = receiver.systemd_unit()
        self.assertEqual(file, Path(self.tmp.name) / ".config/systemd/user/call-bridge-receiver.service")
        self.assertIn('ExecStart="/home/k ite/py%%thon" "-m" "call_bridge.receiver"', body)
        self.assertIn('Environment="PATH=/opt/homebrew/bin:/usr/bin"', body)
        self.assertIn("WantedBy=default.target", body)

    def test_windows_task_starts_at_logon_in_the_interactive_session_without_a_window(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            python = Path(folder) / "python.exe"
            python.write_text("", encoding="utf-8")
            python.with_name("pythonw.exe").write_text("", encoding="utf-8")
            with patch.object(receiver.sys, "executable", str(python)):
                task = receiver.windows_task("FOX\\kite_ & co")
        self.assertIn("<LogonTrigger>", task)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", task)
        self.assertIn("<UserId>FOX\\kite_ &amp; co</UserId>", task)
        self.assertIn(f"<Command>{python.with_name('pythonw.exe')}</Command>", task)
        self.assertIn("<Arguments>-m call_bridge.receiver</Arguments>", task)

    def test_state_says_running_only_while_a_receiver_holds_the_lock(self) -> None:
        with patch.object(Path, "home", return_value=Path(self.tmp.name)):
            self.assertEqual(receiver.state("linux"), {"registered": False, "running": False})
            held = receiver._claim()
            self.addCleanup(os.close, held)
            self.assertIsNone(receiver._claim())
            self.assertEqual(receiver.state("linux"), {"registered": False, "running": True})
        self.assertFalse(receiver.state("sunos5")["registered"])
        with self.assertRaises(DeliveryError):
            receiver.install("sunos5")

    def test_receiver_starts_watching_calls_opened_after_it_started_but_not_exec_calls(self) -> None:
        store = local.LocalStore(Path(self.tmp.name))
        watched: list[str] = []

        class Recorder:
            def __init__(self, _store, receiver=False):
                self.receiver = receiver

            def start(self, session_id, claimed=None):
                watched.append(session_id)

            async def close(self):
                pass

        first, exec_call, later = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        store.add(first, "thread-a", Path(self.tmp.name), "ナユタ")
        store.add(exec_call, "thread-b", Path(self.tmp.name), "ナユタ", "exec")

        async def scenario() -> None:
            task = asyncio.create_task(receiver.run(scans=2))
            await asyncio.sleep(0)
            store.add(later, "", Path(""), "ナユタ", "manual", harness="grok", cwd=self.tmp.name)
            await task

        with patch.object(local, "Watchers", Recorder), patch.object(receiver, "_SCAN_SECONDS", 0.01):
            asyncio.run(scenario())
        self.assertEqual(set(watched), {first, later})


class ReceiverReloadTest(unittest.TestCase):
    def test_macos_reinstall_waits_for_the_old_agent_to_be_gone_before_registering(self) -> None:
        calls: list[str] = []
        still_there = {"n": 2}

        def run(command, accept=(0,)):
            calls.append(command[1])
            code = 0
            if command[1] == "print":
                code = 0 if still_there["n"] > 0 else 113
                still_there["n"] -= 1
            return subprocess.CompletedProcess(command, code, "", "")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CALL_BRIDGE_STATE": tmp}), \
             patch.object(Path, "home", return_value=Path(tmp)), patch.object(receiver, "_run", run), \
             patch.object(receiver.os, "getuid", return_value=501, create=True), \
             patch.object(receiver.time, "sleep", lambda _s: None), \
             patch.object(receiver, "state", return_value={"registered": True, "running": True}):
            self.assertEqual(receiver.install("darwin"), {"registered": True, "running": True})
            self.assertTrue((Path(tmp) / "Library/LaunchAgents/dev.kitepon.call-bridge.receiver.plist").is_file())
        self.assertEqual(calls, ["bootout", "print", "print", "print", "bootstrap"])


class ReceiverLimitsTest(unittest.TestCase):
    def test_file_limit_is_raised_and_never_lowered(self) -> None:
        import resource
        calls: list[tuple[int, int]] = []
        with patch.object(resource, "getrlimit", return_value=(256, resource.RLIM_INFINITY)), \
             patch.object(resource, "setrlimit", side_effect=lambda _kind, value: calls.append(value)):
            receiver.raise_file_limit()
        self.assertEqual(calls, [(4096, resource.RLIM_INFINITY)])
        calls.clear()
        with patch.object(resource, "getrlimit", return_value=(256, 1024)), \
             patch.object(resource, "setrlimit", side_effect=lambda _kind, value: calls.append(value)):
            receiver.raise_file_limit()
        self.assertEqual(calls, [(1024, 1024)])
        calls.clear()
        with patch.object(resource, "getrlimit", return_value=(65536, 65536)), \
             patch.object(resource, "setrlimit", side_effect=lambda _kind, value: calls.append(value)):
            receiver.raise_file_limit()
        self.assertEqual(calls, [])

    def test_store_closes_its_connection_after_each_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = local.LocalStore(Path(tmp))
            with store.connect() as db:
                db.execute("SELECT 1").fetchone()
            with self.assertRaises(Exception):
                db.execute("SELECT 1")  # 閉じた接続は使えない

    def test_receiver_survives_a_round_in_which_the_store_cannot_be_read(self) -> None:
        import sqlite3
        rounds = {"n": 0}

        class Store:
            def __init__(self, _root):
                pass

            def active(self):
                rounds["n"] += 1
                if rounds["n"] == 1:
                    raise sqlite3.OperationalError("unable to open database file")
                return []

        class Quiet:
            def __init__(self, _store, receiver=False):
                pass

            async def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CALL_BRIDGE_STATE": tmp}), \
             patch.object(local, "LocalStore", Store), patch.object(local, "Watchers", Quiet), \
             patch.object(receiver, "_SCAN_SECONDS", 0.01):
            asyncio.run(receiver.run(scans=2))
        self.assertEqual(rounds["n"], 2)


class HarnessSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"CALL_BRIDGE_STATE": self.tmp.name, "CURSOR_HOME": self.tmp.name,
                                      "CALL_BRIDGE_TOKEN": "secret-token"})
        env.start()
        self.addCleanup(env.stop)
        self.commands: list[list[str]] = []
        self.registered = False

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[2] == "add":
            self.registered = True
        if command[2] == "remove":
            self.registered = False
        shown = "call-bridge: python -m call_bridge.local" if self.registered and command[2] in ("get", "list") else ""
        return subprocess.CompletedProcess(command, 0 if command[2] != "get" or self.registered else 1, shown, "")

    def test_cursor_gains_one_entry_and_keeps_the_rest(self) -> None:
        file = Path(self.tmp.name) / "mcp.json"
        file.write_text(json.dumps({"mcpServers": {"other": {"url": "https://example.test/mcp"}}, "extra": 1}),
                        encoding="utf-8")
        self.assertEqual(harness_setup.enable("cursor"), {"harness": "cursor", "status": "registered"})
        saved = json.loads(file.read_text(encoding="utf-8"))
        self.assertEqual(saved["extra"], 1)
        self.assertEqual(saved["mcpServers"]["other"], {"url": "https://example.test/mcp"})
        self.assertEqual(saved["mcpServers"]["call-bridge"], {"command": sys.executable, "args": ["-m", "call_bridge.local"]})
        self.assertEqual(json.loads((Path(self.tmp.name) / "auth.json").read_text(encoding="utf-8")), {"token": "secret-token"})
        self.assertEqual(harness_setup.disable("cursor")["status"], "not_registered")
        self.assertEqual(list(json.loads(file.read_text(encoding="utf-8"))["mcpServers"]), ["other"])

    def test_broken_cursor_file_is_left_untouched(self) -> None:
        file = Path(self.tmp.name) / "mcp.json"
        file.write_text("{壊れた", encoding="utf-8")
        with self.assertRaises(DeliveryError) as caught:
            harness_setup.enable("cursor")
        self.assertEqual((caught.exception.code, file.read_text(encoding="utf-8")), ("HARNESS_CONFIG_INVALID", "{壊れた"))

    def test_claude_code_and_grok_are_registered_through_their_own_cli(self) -> None:
        with patch.object(harness_setup.shutil, "which", side_effect=lambda name: f"/bin/{name}"):
            self.assertEqual(harness_setup.enable("claude-code", self._run)["status"], "registered")
            self.assertEqual(self.commands[1], ["/bin/claude", "mcp", "add", "--scope", "user", "call-bridge", "--",
                                                sys.executable, "-m", "call_bridge.local"])
            self.commands.clear()
            self.assertEqual(harness_setup.enable("grok", self._run)["status"], "registered")
            self.assertEqual(self.commands[1], ["/bin/grok", "mcp", "add", "--scope", "user", "call-bridge",
                                                sys.executable, "--", "-m", "call_bridge.local"])
            self.assertEqual(harness_setup.disable("grok", self._run)["status"], "not_registered")

    def test_without_a_token_nothing_is_registered(self) -> None:
        with patch.dict(os.environ, {"CALL_BRIDGE_TOKEN": ""}), self.assertRaises(DeliveryError) as caught:
            harness_setup.enable("cursor")
        self.assertEqual(caught.exception.code, "BRIDGE_TOKEN_MISSING")
        self.assertFalse((Path(self.tmp.name) / "mcp.json").exists())
        with patch.object(harness_setup.shutil, "which", return_value=None):
            self.assertEqual(harness_setup.status("grok"), {"harness": "grok", "status": "unavailable"})
        with self.assertRaises(DeliveryError):
            harness_setup.enable("codex")


if __name__ == "__main__":
    unittest.main()
