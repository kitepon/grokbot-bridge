"""Exercise App Server ownership with disposable children, never a real caller."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from call_bridge.codex_delivery import CodexRPC, DeliveryError


class CodexRPCLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "requests.jsonl"
        self.child = self.root / "server.py"
        self.child.write_text('''import json, sys, time
mode, log = sys.argv[1:3]
for line in sys.stdin:
    value = json.loads(line)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + "\\n")
    if mode == "unresponsive":
        time.sleep(60)
    if "id" not in value or mode == "silent":
        continue
    answer = {"id": value["id"]}
    if mode == "reject":
        answer["error"] = {"message": "synthetic initialize rejection"}
    else:
        answer["result"] = {"method": value["method"]}
    print(json.dumps(answer), flush=True)
''', encoding="utf-8")

    async def cleanup_child(self, rpc):
        if rpc.process is not None and rpc.process.returncode is None:
            rpc.process.kill()
            await rpc.process.wait()

    def command(self, mode):
        return patch("call_bridge.codex_delivery.codex_command", return_value=[
            sys.executable, "-u", str(self.child), mode, str(self.log)])

    async def test_initialize_timeout_reaps_child_without_sending_delivery(self):
        rpc = CodexRPC(self.root, timeout=0.15)
        self.addAsyncCleanup(self.cleanup_child, rpc)
        with self.command("silent"):
            with self.assertRaises(DeliveryError) as raised:
                async with rpc:
                    self.fail("initialize must not enter the body")
        self.assertEqual(raised.exception.code, "CODEX_REQUEST_TIMEOUT")
        self.assertFalse(raised.exception.outcome_unknown)
        self.assertIsNotNone(rpc.process.returncode)
        self.assertEqual([json.loads(line)["method"] for line in self.log.read_text().splitlines()],
                         ["initialize"])

    async def test_initialize_rejection_reaps_child_and_preserves_error(self):
        rpc = CodexRPC(self.root)
        self.addAsyncCleanup(self.cleanup_child, rpc)
        with self.command("reject"):
            with self.assertRaises(DeliveryError) as raised:
                async with rpc:
                    self.fail("rejected initialize must not enter the body")
        self.assertEqual(raised.exception.code, "CODEX_REQUEST_REJECTED")
        self.assertIsNotNone(rpc.process.returncode)

    async def test_initialize_timeout_reaps_a_child_that_ignores_stdin_close(self):
        rpc = CodexRPC(self.root, timeout=0.15)
        self.addAsyncCleanup(self.cleanup_child, rpc)
        with self.command("unresponsive"):
            with self.assertRaises(DeliveryError) as raised:
                async with rpc:
                    self.fail("initialize must not enter the body")
        self.assertEqual(raised.exception.code, "CODEX_REQUEST_TIMEOUT")
        self.assertIsNotNone(rpc.process.returncode)

    async def test_cancellation_during_initialize_reaps_owned_child(self):
        rpc = CodexRPC(self.root)
        self.addAsyncCleanup(self.cleanup_child, rpc)
        with self.command("silent"):
            task = asyncio.create_task(rpc.__aenter__())
            while rpc.process is None:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(rpc.process.returncode)

    async def test_successful_handshake_and_request_close_normally(self):
        rpc = CodexRPC(self.root)
        self.addAsyncCleanup(self.cleanup_child, rpc)
        with self.command("reply"):
            async with rpc:
                self.assertEqual(await rpc.request("thread/read", {"threadId": "synthetic"}),
                                 {"method": "thread/read"})
        self.assertIsNotNone(rpc.process.returncode)
        self.assertEqual([json.loads(line)["method"] for line in self.log.read_text().splitlines()],
                         ["initialize", "initialized", "thread/read"])
