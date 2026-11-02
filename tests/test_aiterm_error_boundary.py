"""MCP cleanup must not hide the known delivery outcome."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import anyio
import pytest

from call_bridge import aiterm

CONNECT = aiterm.connect

@asynccontextmanager
async def grouped_stdio(_parameters):
    async with anyio.create_task_group():
        yield None, None

class GroupedSession:
    def __init__(self, *_args):
        self.group = anyio.create_task_group()
    async def __aenter__(self):
        await self.group.__aenter__()
        return self
    async def __aexit__(self, *args):
        return await self.group.__aexit__(*args)
    async def initialize(self):
        pass

@pytest.mark.asyncio
@pytest.mark.parametrize('sent', [False, None, True])
async def test_nested_groups_preserve_original_delivery_outcome(sent):
    original = aiterm.AitermError('AITERM_LAUNCH_FAILED', 'preflight', sent=sent)
    with patch.object(aiterm, 'aiterm_command', return_value=['fake']), \
         patch.object(aiterm, 'stdio_client', grouped_stdio), \
         patch.object(aiterm, 'ClientSession', GroupedSession):
        with pytest.raises(aiterm.AitermError) as caught:
            async with CONNECT():
                raise original
    assert caught.value is original
    assert caught.value.outcome_unknown is (sent is not False)

@pytest.mark.asyncio
async def test_multiple_errors_are_not_discarded():
    original = ExceptionGroup('mixed', [aiterm.AitermError('FAIL', 'x', sent=False), RuntimeError('cleanup')])
    with patch.object(aiterm, 'aiterm_command', return_value=['fake']), \
         patch.object(aiterm, 'stdio_client', grouped_stdio), \
         patch.object(aiterm, 'ClientSession', GroupedSession):
        with pytest.raises(ExceptionGroup):
            async with CONNECT():
                raise original

@pytest.mark.asyncio
async def test_failed_launch_is_recorded_instead_of_left_starting(tmp_path):
    from call_bridge import local, relaunch
    sid = 'b6c9d820-3be1-4a28-bc09-5867e161fe5b'
    store = local.LocalStore(tmp_path)
    store.add(sid, 'original', tmp_path, 'member', delivery_mode='channel', harness='claude-code', cwd=str(tmp_path))
    failure = aiterm.AitermError('AITERM_LAUNCH_FAILED', 'auth preflight', sent=False)
    with patch.object(aiterm, 'aiterm_command', return_value=['fake']), \
         patch.object(aiterm, 'stdio_client', grouped_stdio), \
         patch.object(aiterm, 'ClientSession', GroupedSession), \
         patch.object(aiterm.Aiterm, 'sessions', AsyncMock(return_value={})), \
         patch.object(aiterm.Aiterm, 'launch', AsyncMock(side_effect=failure)), \
         patch.object(relaunch, 'state_root', return_value=tmp_path):
        with pytest.raises(aiterm.AitermError):
            await relaunch.hand_over(store, store.subscription(sid), ['test body'], 'closed', connect=CONNECT)
    with store.connect() as db:
        row = db.execute('SELECT state,detail FROM relaunches ORDER BY at DESC LIMIT 1').fetchone()
    assert row['state'] == 'failed'
    assert row['detail'] == str(failure)
    assert store.subscription(sid)['thread_id'] == 'original'
