"""試験は、動かした端末へ何も立てず、何も登録しない。

本物の Aiterm へつなぐと、席（本物の AI の会話）が立つ。試験が自分の代わりを渡した時だけ、そこへつながる。
"""

from __future__ import annotations

from unittest import mock

import pytest


def _refuse_real_aiterm():
    raise AssertionError("試験から本物の Aiterm へつなごうとしました。aiterm.connect を試験用に差し替えてください")


def _refuse_real_claude_settings():
    raise AssertionError("試験から本物の Claude Code・Cursor の設定へ触ろうとしました。"
                         "claude_channel.settings_file・cursor_hooks_file を試験用に差し替えてください")


@pytest.fixture(autouse=True)
def _nothing_real():
    with mock.patch("call_bridge.setup._receiver", return_value="running"), \
         mock.patch("call_bridge.aiterm.connect", _refuse_real_aiterm), \
         mock.patch("call_bridge.claude_channel.settings_file", _refuse_real_claude_settings), \
         mock.patch("call_bridge.claude_channel.cursor_hooks_file", _refuse_real_claude_settings):
        yield
