"""試験は、動かした端末へ常駐の受け取り係を登録しない。"""

from __future__ import annotations

from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _no_real_receiver():
    with mock.patch("call_bridge.setup._receiver", return_value="running"):
        yield
