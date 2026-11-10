"""A certainly undelivered reply is retried on its own, without a newer reply (2026-11-10, seq55)."""
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from call_bridge import local
DeliveryError = local.DeliveryError

SID = '54b9ca24-d41a-40df-a9dc-3b972737dae7'


class Stop(Exception):
    pass


def _client(polls, now=None, times=()):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={'ok': True}, request=httpx.Request('POST', 'https://t/'))
    count = {'n': 0}

    async def get(*_args, **_kwargs):
        count['n'] += 1
        if now is not None and count['n'] <= len(times):
            now['t'] = times[count['n'] - 1]
        if count['n'] > polls:
            raise Stop()
        message = {'seq': 55, 'message': 'body', 'fetched': False}
        return httpx.Response(200, json={'ok': True, 'status': 'open', 'messages': [message]},
                              request=httpx.Request('GET', 'https://t/'))
    client.get.side_effect = get
    return client


async def _run(store, watchers, times, polls, channel):
    """The clock moves only when the watcher polls: poll n happens at times[n-1]."""
    now = {'t': 0.0}
    with patch.object(local.httpx, 'AsyncClient', return_value=_client(polls, now, times)), \
         patch.object(local, '_headers', return_value={}), \
         patch.object(local.asyncio, 'sleep', AsyncMock()), \
         patch.object(local.time, 'monotonic', side_effect=lambda: now['t']), \
         patch.object(local.Watchers, '_channel', channel):
        with pytest.raises(Stop):
            await watchers._watch_claimed(SID)


def _store(tmp_path):
    store = local.LocalStore(tmp_path)
    store.add(SID, 'f9926a2d-8509-498b-b4c6-80804d124b19', Path('.'), 'ナユタ', delivery_mode='channel',
              harness='claude-code')
    return store


@pytest.mark.asyncio
async def test_held_reply_is_retried_after_the_interval_without_a_newer_reply(tmp_path):
    store = _store(tmp_path)
    calls = []

    async def channel(self, subscription, message, delivery_id, text, report):
        calls.append(delivery_id)
        if len(calls) == 1:
            raise DeliveryError('AITERM_LAUNCH_FAILED', 'reason=unrecognized_screen')
        self.store.submitted(SID, message['seq'], 'relaunched')
        return 'next'
    # poll1 at t=0 fails and holds; poll2 at t=10 waits; poll3 after the interval retries and delivers.
    await _run(store, local.Watchers(store, receiver=True), [0.0, 10.0, local._HOLD_RETRY_SECONDS + 1], 3, channel)
    assert len(calls) == 2 and calls[0] == calls[1]  # same deterministic delivery_id, no second row
    row = store.status(SID)['deliveries'][0]
    assert (row['seq'], row['state']) == (55, 'relaunched')
    assert store.subscription(SID)['hold_seq'] is None


@pytest.mark.asyncio
async def test_held_reply_is_not_retried_before_the_interval(tmp_path):
    store = _store(tmp_path)
    channel = AsyncMock(side_effect=DeliveryError('AITERM_LAUNCH_FAILED', 'reason=unrecognized_screen'))
    await _run(store, local.Watchers(store, receiver=True), [0.0, 10.0, 600.0, 1799.0, 1799.9], 5, channel)
    assert channel.await_count == 1
    assert store.subscription(SID)['hold_seq'] == 55


@pytest.mark.asyncio
async def test_restarted_receiver_retries_a_held_reply_once_right_away(tmp_path):
    store = _store(tmp_path)
    store.reserve(SID, 55)
    store.hold(SID, 55, 55, 'AITERM_LAUNCH_FAILED: reason=unrecognized_screen')
    channel = AsyncMock(side_effect=DeliveryError('AITERM_LAUNCH_FAILED', 'again'))
    await _run(store, local.Watchers(store, receiver=True), [0.0, 10.0, 20.0], 3, channel)
    assert channel.await_count == 1
    assert store.status(SID)['deliveries'][0]['error'] == 'AITERM_LAUNCH_FAILED: again'


@pytest.mark.asyncio
async def test_unknown_outcome_is_still_never_retried(tmp_path):
    store = _store(tmp_path)
    channel = AsyncMock(side_effect=DeliveryError('AITERM_SEND_FAILED', 'unknown', outcome_unknown=True))
    with patch.object(local.httpx, 'AsyncClient', return_value=_client(5)), \
         patch.object(local, '_headers', return_value={}), \
         patch.object(local.asyncio, 'sleep', AsyncMock()), \
         patch.object(local.Watchers, '_channel', channel):
        await local.Watchers(store, receiver=True)._watch_claimed(SID)
    assert channel.await_count == 1
    assert store.subscription(SID)['state'] == 'unknown'
