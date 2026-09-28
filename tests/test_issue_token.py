"""scripts/issue_token.py: issue, refuse, rotate, and load with the bridge."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path

from call_bridge.auth import Authenticator

_SPEC = importlib.util.spec_from_file_location(
    "issue_token", Path(__file__).resolve().parents[1] / "scripts" / "issue_token.py")
issue_token = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(issue_token)  # type: ignore[union-attr]


class IssueTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.tokens = self.dir / "tokens.json"

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = issue_token.main(["--tokens-file", str(self.tokens), *argv])
        return code, out.getvalue(), err.getvalue()

    def _names(self) -> list[str]:
        return [t["name"] for t in json.loads(self.tokens.read_text())["tokens"]]

    def test_issue_writes_token_600_and_bridge_accepts_it(self) -> None:
        out = self.dir / "bellteam.token"
        code, stdout, _ = self._run("--name", "bellteam", "--system", "bellteam",
                                    "--caller-id-header", "--out", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.tokens.stat().st_mode), 0o600)
        token = out.read_text().strip()
        self.assertNotIn(token, self.tokens.read_text())
        entry = json.loads(self.tokens.read_text())["tokens"][0]
        self.assertEqual(entry["sha256"], hashlib.sha256(token.encode()).hexdigest())
        principal = Authenticator("", str(self.tokens)).authenticate(
            {"authorization": f"Bearer {token}", "x-call-bridge-caller-id": "bot-a"})
        self.assertEqual((principal.system, principal.id), ("bellteam", "bot-a"))

    def test_same_name_or_existing_out_stops_without_changes(self) -> None:
        out = self.dir / "grok.token"
        self._run("--name", "grokbot", "--system", "grokbot", "--out", str(out))
        before = (self.tokens.read_text(), out.read_text())
        code, _, err = self._run("--name", "grokbot", "--system", "grokbot", "--out", str(self.dir / "other"))
        self.assertEqual(code, 1)
        self.assertIn("--replace", err)
        self.assertFalse((self.dir / "other").exists())
        code, _, err = self._run("--name", "second", "--system", "grokbot", "--out", str(out))
        self.assertEqual(code, 1)
        self.assertEqual((self.tokens.read_text(), out.read_text()), before)

    def test_replace_rotates_the_token_and_keeps_other_entries(self) -> None:
        out = self.dir / "grok.token"
        self._run("--name", "local-mac", "--system", "local", "--out", str(self.dir / "mac.token"))
        self._run("--name", "grokbot", "--system", "grokbot", "--out", str(out))
        old = out.read_text().strip()
        code, _, _ = self._run("--name", "grokbot", "--system", "grokbot", "--out", str(out), "--replace")
        self.assertEqual(code, 0)
        new = out.read_text().strip()
        self.assertNotEqual(old, new)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(sorted(self._names()), ["grokbot", "local-mac"])
        auth = Authenticator("", str(self.tokens))
        self.assertIsNone(auth.authenticate({"authorization": f"Bearer {old}"}))
        self.assertEqual(auth.authenticate({"authorization": f"Bearer {new}"}).system, "grokbot")

    def test_stdout_and_header_format(self) -> None:
        code, stdout, _ = self._run("--name", "grokbot", "--system", "grokbot", "--out", "-")
        self.assertEqual(code, 0)
        token = stdout.strip()
        self.assertEqual(Authenticator("", str(self.tokens)).authenticate(
            {"authorization": f"Bearer {token}"}).system, "grokbot")
        out = self.dir / "headers"
        self._run("--name", "bellteam", "--system", "bellteam", "--out", str(out), "--format", "header")
        self.assertRegex(out.read_text(), r"^Authorization: Bearer \S+\n$")

    def test_existing_mode_is_kept_and_bad_input_is_refused(self) -> None:
        self.tokens.write_text('{"tokens": []}\n')
        self.tokens.chmod(0o640)
        self._run("--name", "ops", "--ops", "--out", str(self.dir / "ops.token"))
        self.assertEqual(stat.S_IMODE(self.tokens.stat().st_mode), 0o640)
        for argv in (["--name", "x", "--out", "-"],
                     ["--name", "x", "--ops", "--caller-id-header", "--out", "-"]):
            with self.subTest(argv):
                self.assertEqual(self._run(*argv)[0], 1)
        self.tokens.write_text("{")
        code, _, err = self._run("--name", "y", "--system", "local", "--out", "-")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", err)
        self.assertEqual(self.tokens.read_text(), "{")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith(".tokens")), [])


if __name__ == "__main__":
    unittest.main()
