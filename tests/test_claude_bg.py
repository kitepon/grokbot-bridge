"""Claude Code に止められた裏の会話（claude --bg）を、同じ会話のまま起こす所。claude の命令は、試験用の代わりを流す。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from call_bridge import claude_bg
from call_bridge.codex_delivery import DeliveryError

SESSION = "e50377e1-df36-44f7-a741-24318d7ba9f4"
CALLS = ["ad1aebf7-90bf-46ff-8e70-7f6bf94a643d", "5e2074e8-58bc-4626-840d-9a548e4429bd"]

# claude の代わり。受け取った引数とフォルダを file へ書き、決めた出力と終わりの番号を返す。
_STUB = """\
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
plan = json.load(open(os.path.join(here, "plan.json"), encoding="utf-8"))
with open(os.path.join(here, "seen.jsonl"), "a", encoding="utf-8") as seen:
    seen.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd()}, ensure_ascii=False) + "\\n")
answer = plan["agents"] if sys.argv[1:2] == ["agents"] else plan["wake"]
if answer.get("sleep"):
    import time
    time.sleep(answer["sleep"])
sys.stderr.write(answer.get("stderr", ""))
sys.stdout.write(answer.get("stdout", ""))
sys.exit(answer.get("exit", 0))
"""


class ClaudeBackgroundTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "OpenLogicool"
        self.folder.mkdir()
        (self.root / "stub.py").write_text(_STUB, encoding="utf-8")
        if os.name == "nt":
            self.stub = self.root / "claude.cmd"
            self.stub.write_text(f'@"{sys.executable}" "{self.root / "stub.py"}" %*\r\n', encoding="utf-8")
        else:
            self.stub = self.root / "claude"
            self.stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.root / "stub.py"}" "$@"\n', encoding="utf-8")
            self.stub.chmod(0o755)
        patcher = patch.object(claude_bg, "_claude", lambda: str(self.stub))
        patcher.start()
        self.addCleanup(patcher.stop)
        self._plan([])

    def _plan(self, rows: list[dict], wake: dict | None = None) -> None:
        plan = {"agents": {"stdout": json.dumps(rows, ensure_ascii=False), "stderr": "warning: not json\n"},
                "wake": wake or {"stderr": "note: woke session e50377e1 with its saved options (--name).\n",
                                 "stdout": "backgrounded · e50377e1 · OpenLogicool\n  claude agents   list sessions\n"}}
        (self.root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def _seen(self) -> list[dict]:
        file = self.root / "seen.jsonl"
        return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()] if file.exists() else []

    def _job(self, **more) -> dict:
        return {"id": SESSION[:8], "cwd": str(self.folder), "kind": "background", "sessionId": SESSION, "state": "done", **more}

    async def test_stopped_background_conversation_is_found_with_its_folder(self):
        self._plan([{"pid": 1, "kind": "interactive", "sessionId": "other", "cwd": "/x"}, self._job()])
        found = await claude_bg.background(SESSION)
        self.assertEqual(found, claude_bg.Background(SESSION, str(self.folder), False, "done"))
        self.assertTrue(found.retired)  # 番を終えた後で止まっている。Claude Code が自分で止めた会話はこの形
        self.assertEqual(self._seen()[0]["args"], ["agents", "--json", "--all"])  # 読むだけの一覧

    async def test_conversation_running_in_the_background_or_in_the_app_counts_as_running(self):
        self._plan([self._job(pid=77960, status="idle", state="blocked")])
        self.assertTrue((await claude_bg.background(SESSION)).running)
        self._plan([self._job(pid=77960, status="idle")])
        self.assertFalse((await claude_bg.background(SESSION)).retired)  # 済んだ会話でも、動いている間は起こさない
        # 裏では止まっていて、同じ会話をアプリが開いている（fox で多い形）
        self._plan([self._job(state="stopped"), {"pid": 30380, "kind": "interactive", "sessionId": SESSION, "cwd": str(self.folder)}])
        self.assertTrue((await claude_bg.background(SESSION)).running)

    async def test_conversation_stopped_in_the_middle_of_its_work_is_not_one_to_wake(self):
        for state in ("stopped", "working", "blocked"):
            with self.subTest(state=state):
                self._plan([self._job(state=state)])
                found = await claude_bg.background(SESSION)
                self.assertEqual((found.running, found.state, found.retired), (False, state, False))

    async def test_conversation_that_is_not_a_background_one_is_left_alone(self):
        self._plan([{"pid": 40556, "kind": "interactive", "sessionId": SESSION, "cwd": str(self.folder)}])
        self.assertIsNone(await claude_bg.background(SESSION))
        self._plan([])
        self.assertIsNone(await claude_bg.background(SESSION))  # 消された会話
        (self.root / "plan.json").write_text(json.dumps({"agents": {"stdout": "not json"}, "wake": {}}), encoding="utf-8")
        self.assertIsNone(await claude_bg.background(SESSION))
        (self.root / "plan.json").write_text(json.dumps({"agents": {"stdout": "[]", "exit": 1}, "wake": {}}), encoding="utf-8")
        self.assertIsNone(await claude_bg.background(SESSION))
        with patch.object(claude_bg, "_claude", lambda: None):  # claude が入っていない端末
            self.assertIsNone(await claude_bg.background(SESSION))
        with patch.object(claude_bg, "_claude", lambda: str(self.root / "missing")):
            self.assertIsNone(await claude_bg.background(SESSION))

    async def test_wake_continues_the_same_conversation_in_its_folder_with_one_line(self):
        started = await claude_bg.wake(claude_bg.Background(SESSION, str(self.folder), False), CALLS)
        self.assertEqual(started, SESSION[:8])
        seen = self._seen()[0]
        self.assertEqual(seen["args"][:3], ["--bg", "--resume", SESSION])
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(self.folder))
        prompt = seen["args"][3]
        self.assertEqual(prompt, claude_bg.wake_prompt(CALLS))  # 引数を通っても1字も崩れない
        self.assertEqual(len(seen["args"]), 4)
        for call in CALLS:
            self.assertIn(f"session_id={call}", prompt)
        self.assertIn("call_adopt", prompt)
        for forbidden in "\n\r%&|<>^\"!":  # 改行と、Windows の cmd が読み替える字は入れない
            self.assertNotIn(forbidden, prompt)

    async def test_wake_that_claude_refuses_raises_and_names_what_it_said(self):
        found = claude_bg.Background(SESSION, str(self.folder), False)
        self._plan([], wake={"stderr": "Workspace not trusted. Run `claude` once and accept the trust prompt.\n", "exit": 1})
        with self.assertRaises(DeliveryError) as raised:
            await claude_bg.wake(found, CALLS)
        self.assertEqual(raised.exception.code, "CLAUDE_BG_WAKE_FAILED")
        self.assertIn("Workspace not trusted", str(raised.exception))
        with patch.object(claude_bg, "_claude", lambda: str(self.root / "missing")):  # 命令を流せない。何も始まっていない
            with self.assertRaises(DeliveryError) as raised:
                await claude_bg.wake(found, CALLS)
        self.assertEqual(raised.exception.code, "CLAUDE_BG_UNAVAILABLE")

    async def test_wake_whose_outcome_cannot_be_read_is_not_called_a_failure(self):
        """起きた印を読めないだけで「起こせなかった」と決めると、起きた会話と席の両方が同じ返信で動く。"""
        found = claude_bg.Background(SESSION, str(self.folder), False)
        self._plan([], wake={"stdout": "started in the background (a4c499de)\n"})  # 0 で終わったが、書き方が違う
        self.assertEqual(await claude_bg.wake(found, CALLS), "")
        self._plan([], wake={"sleep": 5})
        with patch.object(claude_bg, "_TIMEOUT", 0.5):  # 終わらないので止めた。どこまで進んだかは分からない
            self.assertEqual(await claude_bg.wake(found, CALLS), "")

    async def test_wake_is_refused_before_claude_runs_when_the_arguments_are_not_plain_ids(self):
        for found, calls, code in (
                (claude_bg.Background("not-a-uuid", str(self.folder), False), CALLS, "CLAUDE_BG_ARGUMENT"),
                (claude_bg.Background(SESSION, str(self.folder), False), ["x & calc"], "CLAUDE_BG_ARGUMENT"),
                (claude_bg.Background(SESSION, str(self.folder), False), [], "CLAUDE_BG_ARGUMENT"),
                (claude_bg.Background(SESSION, str(self.root / "gone"), False), CALLS, "CLAUDE_BG_FOLDER_MISSING")):
            with self.subTest(code=code, calls=calls):
                with self.assertRaises(DeliveryError) as raised:
                    await claude_bg.wake(found, calls)
                self.assertEqual(raised.exception.code, code)
        self.assertEqual(self._seen(), [])

    async def test_copy_started_for_a_running_conversation_is_reported_by_its_own_id(self):
        self._plan([], wake={"stdout": "note: session is already running; started a copy\nbackgrounded · 0a1b2c3d · OpenLogicool\n"})
        started = await claude_bg.wake(claude_bg.Background(SESSION, str(self.folder), False), CALLS)
        self.assertEqual(started, "0a1b2c3d")
        self.assertFalse(SESSION.startswith(started))
