"""別経路で済んだ旧依頼を、配送成功とは記録せず、明示的に失効させる。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import httpx

from . import local
from .codex_delivery import CodexRPC, DeliveryError, state_root


def _refuse() -> None:
    raise DeliveryError("REPLY_SUPERSEDE_UNSAFE", "通話・予約・キューの安全条件が揃いません。失効していません")


async def _queue_absent(subscription: dict, seqs: list[int]) -> None:
    ids = {str(uuid.uuid5(local._DELIVERY_NAMESPACE, f"{subscription['session_id']}:{seq}")) for seq in seqs}
    home = Path(subscription["codex_home"]).resolve()
    directory = state_root() / 'codex-parent-hooks' / 'inputs' / hashlib.sha256(os.fsencode(home)).hexdigest() / subscription['thread_id']
    def check_records():
        # settledは成功の印ではないが、送信処理の終了前に記録へ触れない。
        for delivery_id in ids:
            if (directory / 'claims' / f'{delivery_id}.json').exists():
                _refuse()
            if ((directory / 'pending' / f'{delivery_id}.json').exists()
                    and not (directory / 'settled' / f'{delivery_id}.json').exists()):
                _refuse()
    check_records()
    async with CodexRPC(home) as rpc:
        cursor = None
        seen = set()
        for _ in range(100):
            page = await rpc.request('thread/queue/list', {'threadId': subscription['thread_id'], 'cursor': cursor, 'limit': 100})
            if not isinstance(page, dict) or 'nextCursor' not in page:
                _refuse()
            rows = page.get('data')
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                _refuse()
            if any(row.get('clientUserMessageId') in ids for row in rows):
                _refuse()
            cursor = page.get('nextCursor')
            if cursor is None:
                check_records()
                return
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                _refuse()
            seen.add(cursor)
    _refuse()


async def supersede(session_id: str, seqs: list[int], reason: str,
                    expected_thread: str, expected_after: int, expected_hold: int) -> dict:
    uuid.UUID(session_id)
    uuid.UUID(expected_thread)
    if (not seqs or any(type(seq) is not int or seq <= expected_after or seq > expected_hold for seq in seqs)
            or seqs != sorted(set(seqs)) or not reason.strip() or len(reason) > 500):
        _refuse()
    store = local.store
    before = store.subscription(session_id)
    if ((before['state'], before['harness'], before['delivery_mode'], before['thread_id'], before['after_seq'], before['hold_seq'])
            != ('active', 'codex', 'queue', expected_thread, expected_after, expected_hold)):
        _refuse()
    with store.connect() as db:
        deliveries = [dict(row) for row in db.execute('SELECT * FROM deliveries WHERE session_id=? ORDER BY seq', (session_id,))]
    pending = [row for row in deliveries if row['seq'] > expected_after]
    if (not pending or pending[0]['seq'] != seqs[0]
            or not (pending[0]['error'] or '').startswith('CODEX_DELIVERY_DUPLICATE:')
            or any(row['state'] != 'failed' or row['seq'] not in seqs for row in pending)
            or any(row['delivery_id'] != str(uuid.uuid5(local._DELIVERY_NAMESPACE,
                       f"{session_id}:{row['seq']}")) for row in pending)
            or any(row['state'] == 'submitted' and not row['confirmed'] for row in deliveries)):
        _refuse()
    if not store.ask_to_yield(session_id):
        _refuse()
    fd = None
    try:
        deadline = time.monotonic() + local._YIELD_SECONDS
        while fd is None:
            fd = local._claim_session(session_id)
            if fd is not None:
                break
            if time.monotonic() >= deadline:
                raise DeliveryError('CALL_BUSY', '見張りの鍵を取れません。失効していません')
            await asyncio.sleep(local._YIELD_POLL_SECONDS)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(local._rest_url(session_id), headers=local._headers(),
                                        params={'party': 'local', 'after_seq': expected_after, 'peek': '1'})
            response.raise_for_status()
            value = response.json()
        if (not local._valid_poll_result(value, session_id, 'local', expected_after)
                or value.get('ok') is not True or value['status'] == 'hungup'
                or [row['seq'] for row in value['messages'] if row['seq'] <= max(seqs)] != seqs):
            _refuse()
        await _queue_absent(before, seqs)
        # 公式キュー等を読んでいる間の付替え・停止・予約追加も、transaction内で拒否する。
        store.supersede({**before, 'state': 'yielding'}, deliveries, seqs, reason.strip())
        return {'session_id': session_id, 'superseded_seqs': seqs, 'after_seq': max(seqs),
                'delivery_success_asserted': False}
    finally:
        try:
            store.resume(session_id)
        finally:
            if fd is not None:
                os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--seq', type=int, action='append', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--expected-thread', required=True)
    parser.add_argument('--expected-after', type=int, required=True)
    parser.add_argument('--expected-hold', type=int, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(supersede(args.session, args.seq, args.reason, args.expected_thread,
                                      args.expected_after, args.expected_hold))
    except (DeliveryError, ValueError, OSError, sqlite3.Error, httpx.HTTPError) as exc:
        parser.exit(1, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
