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
from unittest import mock

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

    def test_empty_name_or_id_is_refused_before_writing(self) -> None:
        for argv in (["--name", "", "--system", "local"],
                     ["--name", "   ", "--system", "local"],
                     ["--name", "x", "--system", "bellteam", "--id", " "]):
            with self.subTest(argv):
                code, _, err = self._run(*argv, "--out", "-")
                self.assertEqual(code, 1)
                self.assertIn("must not be empty", err)
                self.assertFalse(self.tokens.exists())
        code, _, _ = self._run("--name", "  mac  ", "--system", "local", "--id", " dev ", "--out", "-")
        self.assertEqual(code, 0)
        entry = json.loads(self.tokens.read_text())["tokens"][0]
        self.assertEqual((entry["name"], entry["id"]), ("mac", "dev"))

    def test_result_is_checked_with_the_bridge_loader(self) -> None:
        self.tokens.write_text(json.dumps({"tokens": [
            {"name": "old", "sha256": "a" * 64, "system": "grokbot", "ops": "false"}]}))
        before = self.tokens.read_text()
        code, stdout, err = self._run("--name", "new", "--system", "local", "--out", "-")
        self.assertEqual(code, 1)
        self.assertIn("would not load", err)
        self.assertEqual(stdout, "")
        self.assertEqual(self.tokens.read_text(), before)

    def _rotate_failing_on(self, failing: Path) -> tuple[str, str]:
        out = self.dir / "grok.token"
        self._run("--name", "grokbot", "--system", "grokbot", "--out", str(out))
        old_token, old_tokens = out.read_text().strip(), self.tokens.read_text()
        real_replace = issue_token.os.replace

        def replace(src, dst):
            if Path(dst) == failing:
                raise OSError("disk full")
            return real_replace(src, dst)

        with mock.patch.object(issue_token.os, "replace", replace):
            code, _, err = self._run("--name", "grokbot", "--system", "grokbot", "--out", str(out), "--replace")
        self.assertEqual(code, 1, err)
        self.assertEqual(out.read_text().strip(), old_token)
        self.assertEqual(self.tokens.read_text(), old_tokens)
        self.assertEqual(Authenticator("", str(self.tokens)).authenticate(
            {"authorization": f"Bearer {old_token}"}).system, "grokbot")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".")], [])
        return old_token, old_tokens

    def test_failed_rotation_keeps_the_old_token_when_the_tokens_file_fails(self) -> None:
        self._rotate_failing_on(self.tokens)

    def test_failed_rotation_restores_the_tokens_file_when_out_fails(self) -> None:
        self._rotate_failing_on(self.dir / "grok.token")

    def test_closed_stdout_registers_nothing(self) -> None:
        class Closed(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError("closed")

        self._run("--name", "grokbot", "--system", "grokbot", "--out", str(self.dir / "grok.token"))
        before = self.tokens.read_text()
        err = io.StringIO()
        with contextlib.redirect_stdout(Closed()), contextlib.redirect_stderr(err):
            code = issue_token.main(["--tokens-file", str(self.tokens), "--name", "grokbot",
                                     "--system", "grokbot", "--out", "-", "--replace"])
        self.assertEqual(code, 1)
        self.assertIn("nothing was registered", err.getvalue())
        self.assertEqual(self.tokens.read_text(), before)


if __name__ == "__main__":
    unittest.main()
