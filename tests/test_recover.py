"""失効は成功ではない。旧返信を再送せず、後続だけを通常配送する。"""
import hashlib
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from httpx import AsyncClient as HTTPClient

from call_bridge import local, recover
from call_bridge.codex_delivery import DeliveryError

SID = '6075dbe9-89df-4baa-9bc2-125190794c64'
THREAD = '01a17233-751d-79b2-94a4-a476d8cb8569'
SEQS = [675, 676, 677]


class RecoverTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, CALL_BRIDGE_STATE=str(self.root), CALL_BRIDGE_TOKEN='test')
        self.env.start(); self.addCleanup(self.env.stop)
        self.store = local.LocalStore(self.root)
        self.store.add(SID, THREAD, self.root, 'member')
        self.store.reserve(SID, 675)
        self.store.hold(SID, 675, 679, 'CODEX_DELIVERY_DUPLICATE: old ownership record')
        with self.store.connect() as db:
            db.execute('UPDATE subscriptions SET after_seq=673 WHERE session_id=?', (SID,))
        self.patch_store = patch.object(local, 'store', self.store)
        self.patch_store.start(); self.addCleanup(self.patch_store.stop)
        self.rpc = SimpleNamespace(request=AsyncMock(return_value={'data': [], 'nextCursor': None}))
        context = AsyncMock()
        context.__aenter__.return_value = self.rpc
        p = patch.object(recover, 'CodexRPC', return_value=context)
        p.start(); self.addCleanup(p.stop)
        self.http_calls = []
        def respond(request):
            self.http_calls.append(request)
            return httpx.Response(200, json={'ok': True, 'status': 'open', 'session_id': SID, 'party': 'local',
                'latest_seq': 679, 'messages': [{'seq': seq, 'from_party': 'member', 'message': 'body'}
                                               for seq in [675, 676, 677, 678, 679]]})
        p = patch.object(recover.httpx, 'AsyncClient', side_effect=lambda **kw: HTTPClient(
            transport=httpx.MockTransport(respond), **kw))
        p.start(); self.addCleanup(p.stop)

    async def apply(self, **kwargs):
        return await recover.supersede(**{'session_id': SID, 'seqs': SEQS, 'reason': 'obsolete: handled elsewhere',
            'expected_thread': THREAD, 'expected_after': 673, 'expected_hold': 679, **kwargs})

    def snapshot(self):
        with self.store.connect() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table)]
                    for table in ('subscriptions', 'deliveries', 'superseded_replies')}

    async def test_old_blocker_is_preserved_and_only_new_replies_are_delivered_once(self):
        before = self.snapshot()
        watcher = local.Watchers(self.store)
        report = AsyncMock()
        found = local.talk.Conversation(THREAD, True, 'vscode', None, 'completed')
        with patch.object(local, 'codex_conversation', AsyncMock(return_value=found)), \
             patch.object(local, 'submit_reply', AsyncMock(side_effect=DeliveryError('CODEX_DELIVERY_DUPLICATE', 'old'))):
            self.assertEqual(await watcher._handle(self.store.subscription(SID), {'seq':675, 'message':'old'}, 679, report), 'wait')
        before = self.snapshot()
        result = await self.apply()
        self.assertFalse(result['delivery_success_asserted'])
        self.assertEqual(self.snapshot()['deliveries'], before['deliveries'])
        self.assertEqual(self.store.subscription(SID)['after_seq'], 677)
        self.assertIsNone(self.store.subscription(SID)['hold_seq'])
        self.assertEqual(self.store.subscription(SID)['state'], 'active')
        self.assertTrue(all(request.url.params['peek'] == '1' for request in self.http_calls))
        send = AsyncMock(return_value='injected')
        report.reset_mock()
        with patch.object(local, 'codex_conversation', AsyncMock(return_value=found)), patch.object(local, 'submit_reply', send):
            # Even an explicit stale reserve cannot resubmit the obsolete identities.
            for seq in [675,676,677,678,679,678,679]:
                self.assertEqual(await watcher._handle(self.store.subscription(SID), {'seq':seq, 'message':'reply'}, 679, report), 'next')
        self.assertEqual([c.args[2] for c in send.await_args_list], [str(uuid.uuid5(local._DELIVERY_NAMESPACE, f'{SID}:{seq}')) for seq in [678,679]])
        self.assertEqual([c.args[0] for c in report.await_args_list], [678,679])
        self.assertEqual(self.store.status(SID)['deliveries'][0]['state'], 'failed')
        self.assertEqual([r['seq'] for r in self.store.status(SID)['superseded_replies']], SEQS)
        self.assertEqual(self.store.reserve(SID, 675)[1], 'superseded')

    async def test_mismatched_or_incomplete_request_changes_nothing(self):
        for args in ({'expected_thread':str(uuid.uuid4())}, {'expected_after':672}, {'expected_hold':677},
                     {'seqs':[]}, {'seqs':[675,677]}, {'seqs':[675,676,677,677]},
                     {'seqs':[673,675]}, {'seqs':[675,680]}, {'reason':' '}):
            with self.subTest(args=args):
                before = self.snapshot()
                with self.assertRaises(DeliveryError): await self.apply(**args)
                self.assertEqual(self.snapshot(), before)

    async def test_active_or_unknown_reservations_are_not_cancelled(self):
        for state in ['sending', 'unknown', 'submitted', 'waiting']:
            with self.subTest(state=state):
                with self.store.connect() as db: db.execute('UPDATE deliveries SET state=?', (state,))
                before = self.snapshot()
                with self.assertRaises(DeliveryError): await self.apply()
                self.assertEqual(self.snapshot(), before)

    async def test_paginated_queue_is_refused_without_reading_later_pages(self):
        self.rpc.request.side_effect = [{'data':[], 'nextCursor':'page2'},
            {'data':[{'clientUserMessageId':str(uuid.uuid5(local._DELIVERY_NAMESPACE, f'{SID}:675'))}], 'nextCursor':None}]
        before = self.snapshot()
        with self.assertRaises(DeliveryError): await self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([call.args[0] for call in self.rpc.request.await_args_list], ['thread/queue/list'])

    async def test_malformed_queue_is_refused(self):
        for page in [{'data':None}, {'data':[1], 'nextCursor':None}, {'data':[], 'nextCursor':42},
                     {'data':[{}], 'nextCursor':None}, {'data':[{'clientUserMessageId':7}], 'nextCursor':None},
                     {'data':[], 'nextCursor':False}, {'data':[]}]:
            with self.subTest(page=page):
                self.rpc.request.return_value = page
                before = self.snapshot()
                with self.assertRaises(DeliveryError): await self.apply()
                self.assertEqual(self.snapshot(), before)

    async def test_claim_or_unfinished_sender_is_refused_without_touching_files(self):
        directory = self.root/'codex-parent-hooks'/'inputs'/hashlib.sha256(os.fsencode(self.root.resolve())).hexdigest()/THREAD
        delivery_id = str(uuid.uuid5(local._DELIVERY_NAMESPACE, f'{SID}:675'))
        for kind in ['claims', 'pending']:
            with self.subTest(kind=kind):
                path = directory/kind/f'{delivery_id}.json'
                path.parent.mkdir(parents=True);path.write_text('retained')
                before = self.snapshot()
                with self.assertRaises(DeliveryError): await self.apply()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(path.read_text(), 'retained')
                path.unlink()

    async def test_changes_during_queue_read_are_preserved_without_superseding(self):
        for change in ["UPDATE subscriptions SET state='paused'", "UPDATE subscriptions SET thread_id='other'",
                       'UPDATE subscriptions SET hold_seq=680', "INSERT INTO deliveries(session_id,seq,delivery_id,state) VALUES('"+SID+"',678,'new','sending')"]:
            with self.subTest(change=change):
                before_sub = self.store.subscription(SID)
                async def read(*args):
                    with self.store.connect() as db: db.execute(change)
                    return {'data':[], 'nextCursor':None}
                self.rpc.request.side_effect = read
                with self.assertRaises(DeliveryError): await self.apply()
                self.assertEqual(self.store.subscription(SID)['after_seq'], 673)
                self.assertEqual(self.store.status(SID)['superseded_replies'], [])
                with self.store.connect() as db:
                    db.execute('DELETE FROM deliveries WHERE seq=678')
                    db.execute('UPDATE subscriptions SET state=?,thread_id=?,hold_seq=?',
                               (before_sub['state'],before_sub['thread_id'],before_sub['hold_seq']))

    async def test_database_failure_rolls_back_all_supersessions(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_supersede BEFORE INSERT ON superseded_replies WHEN NEW.seq=676 BEGIN SELECT RAISE(ABORT, 'test'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError): await self.apply()
        self.assertEqual(self.snapshot(), before)

    async def test_busy_watcher_times_out_and_remains_unchanged(self):
        before = self.snapshot()
        with patch.object(local, '_claim_session', return_value=None), patch.object(local, '_YIELD_SECONDS', 0):
            with self.assertRaisesRegex(DeliveryError, 'CALL_BUSY'): await self.apply()
        self.assertEqual(self.snapshot(), before)

    async def test_genuine_session_lock_must_be_released_before_inspection(self):
        fd = local._claim_session(SID)
        self.assertIsNotNone(fd)
        try:
            task = __import__('asyncio').create_task(self.apply())
            await __import__('asyncio').sleep(0.02)
            self.assertEqual(self.store.subscription(SID)['state'], 'yielding')
            self.rpc.request.assert_not_awaited()
            os.close(fd);fd=None
            await task
            self.assertEqual(self.store.subscription(SID)['after_seq'], 677)
        finally:
            if fd is not None: os.close(fd)

    async def test_hook_claim_appearing_during_queue_read_is_refused(self):
        directory = self.root/'codex-parent-hooks'/'inputs'/hashlib.sha256(os.fsencode(self.root.resolve())).hexdigest()/THREAD/'claims'
        async def read(*args):
            directory.mkdir(parents=True)
            (directory/(str(uuid.uuid5(local._DELIVERY_NAMESPACE, f'{SID}:675'))+'.json')).write_text('in flight')
            return {'data':[], 'nextCursor':None}
        self.rpc.request.side_effect = read
        before = self.snapshot()
        with self.assertRaises(DeliveryError): await self.apply()
        self.assertEqual(self.snapshot(), before)

    async def test_old_pending_and_settled_are_retained_byte_for_byte(self):
        directory = self.root/'codex-parent-hooks'/'inputs'/hashlib.sha256(os.fsencode(self.root.resolve())).hexdigest()/THREAD
        paths = []
        for kind in ['pending','settled']:
            path = directory/kind/(str(uuid.uuid5(local._DELIVERY_NAMESPACE, f'{SID}:675'))+'.json')
            path.parent.mkdir(parents=True);path.write_text('owned record')
            paths.append((path,path.stat().st_ino,path.stat().st_mtime_ns))
        await self.apply()
        for path,inode,mtime in paths:
            self.assertEqual((path.read_text(),path.stat().st_ino,path.stat().st_mtime_ns), ('owned record',inode,mtime))

    async def test_foreign_delivery_identity_is_refused(self):
        with self.store.connect() as db: db.execute('UPDATE deliveries SET delivery_id=?', (str(uuid.uuid4()),))
        before = self.snapshot()
        with self.assertRaises(DeliveryError): await self.apply()
        self.assertEqual(self.snapshot(), before)


    async def test_even_valid_unrelated_queue_rows_prevent_supersession(self):
        self.rpc.request.return_value = {'data':[{'id':'other', 'clientUserMessageId':None}], 'nextCursor':None}
        before = self.snapshot()
        with self.assertRaises(DeliveryError): await self.apply()
        self.assertEqual(self.snapshot(), before)

    async def test_offset_deletion_cannot_hide_target_on_later_page(self):
        target = str(uuid.uuid5(local._DELIVERY_NAMESPACE, f'{SID}:675'))
        queue = [{'id':str(i), 'clientUserMessageId':None} for i in range(100)] + [{'id':'old', 'clientUserMessageId':target}]
        async def read(_method, params):
            offset = int(params['cursor'] or 0)
            rows = queue[offset:offset+100]
            cursor = str(offset+100) if offset+100 < len(queue) else None
            if offset == 0: del queue[0]
            return {'data':rows, 'nextCursor':cursor}
        self.rpc.request.side_effect = read
        before = self.snapshot()
        with self.assertRaises(DeliveryError): await self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rpc.request.await_count, 1)
        self.assertTrue(any(row['clientUserMessageId'] == target for row in queue))
