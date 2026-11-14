"""Public connection handoff: fixed wrapper, provider failures, and credential scope."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from call_bridge import connection


TOKEN = "test-bellteam-credential"
URL = "https://bridge.example:18910/mcp"
PRIVATE = "private-provider-diagnostic-should-not-escape"


class ConnectionExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "deployment"
        self.root.mkdir()
        self.registry = self.root / "auth" / "tokens.json"
        self.registry.parent.mkdir()
        self.provider = [sys.executable, "-c", f"print({TOKEN!r})"]
        self.configure()

    def configure(self, *, url: object = URL, command: object | None = None,
                  tokens_file: object = "auth/tokens.json", entry: dict | None = None) -> None:
        if entry is None:
            entry = {"name": "bellteam", "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                     "system": "bellteam", "caller_id_header": True}
        self.registry.write_text(json.dumps({"tokens": [entry]}), encoding="utf-8")
        config = {"url": url, "tokens_file": tokens_file,
                  "token_command": self.provider if command is None else command}
        (self.root / "connection-export.json").write_text(json.dumps(config), encoding="utf-8")

    @property
    def wrapper(self) -> Path:
        return Path(__file__).resolve().parents[1] / "scripts" / "export_connection.py"

    def run_wrapper(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(self.wrapper), *args], cwd=self.root,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, check=False)

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in sorted(self.root.rglob("*")) if path.is_file()}

    def test_fixed_wrapper_exports_exact_contract_without_mutating_deployment(self) -> None:
        calls = self.base / "provider-calls"
        self.provider = [sys.executable, "-c",
                         f"from pathlib import Path; p=Path({str(calls)!r}); "
                         f"p.write_text((p.read_text() if p.exists() else '') + 'x'); print({TOKEN!r})"]
        self.configure()
        before = self.snapshot()
        result = self.run_wrapper("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"schema": "call-bridge.connection.v1",
                                                     "url": URL, "token": TOKEN})
        self.assertEqual(result.stdout.count("\n"), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(calls.read_text(encoding="utf-8"), "x")

    def test_success_suppresses_provider_stderr(self) -> None:
        self.provider = [sys.executable, "-c", f"import sys; print({TOKEN!r}); print({PRIVATE!r}, file=sys.stderr)"]
        self.configure()
        result = self.run_wrapper("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["token"], TOKEN)
        self.assertNotIn(PRIVATE, result.stdout)

    def test_nonzero_provider_suppresses_private_stdout_and_stderr(self) -> None:
        self.provider = [sys.executable, "-c",
                         f"import sys; print({TOKEN!r}); print({PRIVATE!r}, file=sys.stderr); sys.exit(9)"]
        self.configure()
        before = self.snapshot()
        result = self.run_wrapper("--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(TOKEN, result.stderr)
        self.assertNotIn(PRIVATE, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_rejects_bad_registered_credential_shapes(self) -> None:
        invalid_entries = {
            "wrong system": {"name": "wrong", "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                             "system": "grokbot", "caller_id_header": True},
            "fixed caller id": {"name": "fixed", "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                                "system": "bellteam", "id": "bot-fixed", "caller_id_header": True},
            "no caller header permission": {"name": "no-header", "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                                             "system": "bellteam"},
            "ops": {"name": "ops", "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "ops": True},
            "unregistered": {"name": "other", "sha256": hashlib.sha256(b"different-token").hexdigest(),
                             "system": "bellteam", "caller_id_header": True},
        }
        for label, entry in invalid_entries.items():
            with self.subTest(label=label):
                self.configure(entry=entry)
                before = self.snapshot()
                result = self.run_wrapper("--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(TOKEN, result.stderr)
                self.assertNotIn(PRIVATE, result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_rejects_malformed_urls(self) -> None:
        invalid_urls = ("/mcp", "ftp://bridge.example/mcp", "https://user@bridge.example/mcp",
                        "https://bridge.example/mcp?x=1", "https://bridge.example/mcp#fragment",
                        "https://bridge.example/not-mcp", "https://bridge.example:bad/mcp",
                        "https://bridge.example/mcp ")
        for url in invalid_urls:
            with self.subTest(url=url):
                self.configure(url=url)
                result = self.run_wrapper("--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(TOKEN, result.stderr)

    def test_rejects_malformed_export_config(self) -> None:
        for config in ({"url": URL, "tokens_file": "auth/tokens.json"},
                       {"url": URL, "tokens_file": "auth/tokens.json", "token_command": "provider"},
                       {"url": URL, "tokens_file": "", "token_command": self.provider},
                       {"url": URL, "tokens_file": "auth/tokens.json", "token_command": []}):
            with self.subTest(config=config):
                (self.root / "connection-export.json").write_text(json.dumps(config), encoding="utf-8")
                result = self.run_wrapper("--json")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn(TOKEN, result.stderr)
        (self.root / "connection-export.json").write_text("{", encoding="utf-8")
        result = self.run_wrapper("--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn(TOKEN, result.stderr)

    def test_json_flag_is_required(self) -> None:
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("--json", result.stderr)

    def test_provider_timeout_has_no_output_or_mutation(self) -> None:
        before = self.snapshot()
        with mock.patch.object(connection.subprocess, "run",
                                side_effect=subprocess.TimeoutExpired(self.provider, 45)):
            with self.assertRaises(connection.ExportError) as caught:
                connection.export_connection(self.root)
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
