"""Notifications survive HTTP failure and restart without redelivering the body."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from call_bridge import local

SID = 'f7471d08-093c-4bd5-bc62-6d7908696d5c'

@pytest.mark.asyncio
async def test_failed_receipt_survives_restart_and_latest_state_wins(tmp_path):
    store = local.LocalStore(tmp_path)
    client = AsyncMock()
    client.post.side_effect = httpx.ReadTimeout('offline')
    report = local._Reporter(client, {}, SID, store)
    await report(5, 'submitted', None, 'claude:original')
    await report(5, 'started', None, 'claude:original')
    reopened = local.LocalStore(tmp_path)
    assert reopened.receipt_sessions() == [SID]
    assert len(reopened.pending_receipts(SID)) == 1
    client.post.side_effect = None
    client.post.return_value = httpx.Response(200, json={'ok': True}, request=httpx.Request('POST', 'https://test/'))
    await local._Reporter(client, {}, SID, reopened).flush()
    assert client.post.call_args.kwargs['json']['state'] == 'started'
    assert reopened.pending_receipts(SID) == []

@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{'ok': False}, [], None])
async def test_unaccepted_receipt_is_kept(tmp_path, body):
    store = local.LocalStore(tmp_path)
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json=body, request=httpx.Request('POST', 'https://test/'))
    await local._Reporter(client, {}, SID, store)(5, 'started', None, 'claude:original')
    assert store.pending_receipts(SID)[0]['state'] == 'started'

@pytest.mark.asyncio
async def test_closed_call_flushes_without_poll_or_state_change(tmp_path):
    store = local.LocalStore(tmp_path)
    store.add(SID, 'original', Path('.'), 'member')
    store.stop(SID, 'closed', None)
    store.queue_receipt(SID, 5, 'started', None, 'claude:original')
    before = store.subscription(SID)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={'ok': True}, request=httpx.Request('POST', 'https://test/'))
    with patch.object(local.httpx, 'AsyncClient', return_value=client), patch.object(local, '_headers', return_value={}):
        await local.Watchers(store, receiver=True)._watch_claimed(SID)
    client.get.assert_not_called()
    assert store.subscription(SID) == before
    assert not store.pending_receipts(SID)

def test_old_ack_does_not_erase_a_newer_receipt(tmp_path):
    store = local.LocalStore(tmp_path)
    store.queue_receipt(SID, 5, 'submitted', None, 'claude:original')
    old = store.pending_receipts(SID)[0]
    store.queue_receipt(SID, 5, 'started', None, 'claude:original')
    store.receipt_sent(SID, 5, old['revision'])
    assert store.pending_receipts(SID)[0]['state'] == 'started'

@pytest.mark.asyncio
async def test_emitted_receipt_recovery_preserves_original_records(tmp_path):
    from call_bridge import channel_recover, claude_channel
    channel, owner = '02dc1a68-c78b-4299-9ce6-6f5a7618112b', '28b67a6d-ff5a-4a75-9690-925437c488f2'
    store = local.LocalStore(tmp_path)
    store.add(SID, channel, Path('.'), 'member', delivery_mode='channel', harness='claude-code')
    store.reserve(SID, 42)
    store.submitted(SID, 42)
    before = store.status(SID)
    with patch.object(local, 'store', store), \
         patch.object(claude_channel, 'owner_kind', AsyncMock(return_value=('claude', owner))), \
         patch.object(claude_channel, 'emitted_channel', AsyncMock(return_value=channel)):
        result = await channel_recover.receipt(SID, 42, channel, owner, 42)
    assert result['input_sent'] is False
    assert store.status(SID) == before
    assert store.pending_receipts(SID)[0]['state'] == 'started'

@pytest.mark.asyncio
@pytest.mark.parametrize('state,emitted', [('unknown', True), ('submitted', False)])
async def test_receipt_recovery_refuses_without_proof(tmp_path, state, emitted):
    from call_bridge import channel_recover, claude_channel
    channel, owner = '02dc1a68-c78b-4299-9ce6-6f5a7618112b', '28b67a6d-ff5a-4a75-9690-925437c488f2'
    store = local.LocalStore(tmp_path)
    store.add(SID, channel, Path('.'), 'member', delivery_mode='channel', harness='claude-code')
    store.reserve(SID, 42)
    store.submitted(SID, 42)
    if state == 'unknown': store.stop(SID, 'unknown', 'original', 42)
    before = store.status(SID)
    with patch.object(local, 'store', store), \
         patch.object(claude_channel, 'owner_kind', AsyncMock(return_value=('claude', owner))), \
         patch.object(claude_channel, 'emitted_channel', AsyncMock(return_value=channel if emitted else None)):
        with pytest.raises(local.DeliveryError):
            await channel_recover.receipt(SID, 42, channel, owner, 42)
    assert not store.pending_receipts(SID)
    assert store.status(SID) == before
