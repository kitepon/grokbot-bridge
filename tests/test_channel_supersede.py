import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from call_bridge import channel_recover, claude_channel, local
from call_bridge.codex_delivery import DeliveryError


class SupersedeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,CALL_BRIDGE_STATE=self.tmp.name,CALL_BRIDGE_TOKEN='fake')
        env.start();self.addCleanup(env.stop)
        self.sid,self.current,self.owner,self.other=[str(uuid.uuid4()) for _ in range(4)]
        self.store=local.LocalStore(Path(self.tmp.name))
        self.store.add(self.sid,self.current,Path('.'),'member',delivery_mode='channel',harness='claude-code')
        self.store.advance(self.sid,281)
        self.store.reserve(self.sid,282)
        self.store.stop(self.sid,'unknown','DELIVERY_PREVIOUSLY_STARTED',282)
        self.at=self.store.reserved_at(self.sid,282)
        for ch in [self.current,self.other,'claude-sessions']:
            (Path(self.tmp.name)/'steer-channels'/ch).mkdir(parents=True)
        self.value={'ok':True,'session_id':self.sid,'party':'local','status':'open','latest_seq':283,
                    'messages':[{'seq':seq,'message':'private','from_party':'member','fetched':False} for seq in [282,283]]}
        self.states={}
        self.owners={self.current:self.owner,self.other:self.owner}
        self.mutate=None

    def rows(self, table='deliveries'):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY seq')]

    async def public(self,args):
        if args[0]=='owner':
            return {'kind':'claude','session':self.owners[args[2]]}
        if self.mutate:
            mutation=self.mutate;self.mutate=None;mutation()
        state=self.states.get(args[2])
        return {} if state=='missing' else {'state':state}

    async def apply(self,**changes):
        async def response(*args,**kwargs):
            self.assertEqual(kwargs['params'],{'party':'local','after_seq':281,'peek':'1'})
            return httpx.Response(200,json=self.value,request=httpx.Request('GET','https://example.invalid/poll'))
        client=AsyncMock();client.get.side_effect=response
        args=dict(session_id=self.sid,seq=282,expected_current=self.current,expected_owner=self.owner,
                  expected_after=281,expected_at=self.at,reason='Root explicitly supersedes 282; original effect unknown')
        args.update(changes)
        with patch.object(local,'store',self.store),patch.object(claude_channel,'run',self.public), \
             patch.object(local.httpx,'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=client
            return await channel_recover.supersede(**args)

    async def test_preserves_unknown_and_only_following_reply_is_sent_once(self):
        old=self.rows()
        result=await self.apply()
        self.assertEqual(self.rows(),old)
        self.assertFalse(result['input_sent']);self.assertFalse(result['delivery_success_asserted'])
        self.assertEqual(result['original_outcome'],'unknown')
        self.assertEqual((self.store.subscription(self.sid)['state'],self.store.subscription(self.sid)['after_seq']),('active',282))
        self.assertEqual(self.rows('superseded_replies')[0]['seq'],282)
        send=AsyncMock()
        watcher=local.Watchers(self.store)
        report=AsyncMock()
        with patch.object(claude_channel,'send',send),patch.object(watcher,'_retired',AsyncMock(return_value=None)):
            for seq in [282,283,283]:
                self.assertEqual(await watcher._handle(self.store.subscription(self.sid),{'seq':seq,'message':'body'},283,report),'next')
        send.assert_awaited_once()
        self.assertEqual(self.rows()[0],old[0])
        self.assertEqual(self.store.subscription(self.sid)['after_seq'],283)
        self.assertEqual([c.args[0] for c in report.await_args_list],[283])

    async def test_current_or_other_channel_activity_and_missing_state_refuse(self):
        old=self.rows()
        for channel in [self.current,self.other]:
            for state in ['queued','sending','unknown','emitted','withdrawn','missing','invalid',False]:
                self.states={channel:state}
                with self.assertRaises(DeliveryError):await self.apply()
                self.assertEqual(self.rows(),old);self.assertEqual(self.rows('superseded_replies'),[])

    async def test_wrong_snapshot_and_fetched_or_malformed_poll_refuse(self):
        for change in [{'expected_at':self.at+1},{'seq':283},{'reason':''},{'expected_owner':str(uuid.uuid4())}]:
            with self.assertRaises(DeliveryError):await self.apply(**change)
        for fetched in [True,None,0,'false']:
            self.value['messages'][0]['fetched']=fetched
            with self.assertRaises(DeliveryError):await self.apply()
        self.assertEqual(self.rows('superseded_replies'),[])

    async def test_channel_owner_set_and_ledger_races_refuse(self):
        def moved():self.store.adopt_channel(self.sid,str(uuid.uuid4()),None)
        self.mutate=moved
        with self.assertRaises(DeliveryError):await self.apply()
        self.assertEqual(self.rows('superseded_replies'),[])

    async def test_new_reservation_during_read_refuses(self):
        self.mutate=lambda:self.store.reserve(self.sid,283)
        with self.assertRaises(DeliveryError):await self.apply()
        self.assertEqual(self.rows('superseded_replies'),[])

    async def test_channel_owner_or_set_changes_during_read_refuse(self):
        for change in [lambda:self.owners.update({self.current:str(uuid.uuid4())}),
                       lambda:(Path(self.tmp.name)/'steer-channels'/str(uuid.uuid4())).mkdir()]:
            self.owners[self.current]=self.owner
            self.mutate=change
            with self.assertRaises(DeliveryError):await self.apply()
            self.assertEqual(self.rows('superseded_replies'),[])

    async def test_historical_unknown_does_not_block_later_emitted_recovery(self):
        await self.apply()
        old=self.rows()[0]
        self.store.stop(self.sid,'unknown','DELIVERY_PREVIOUSLY_STARTED')
        self.assertEqual(self.store.unknown_channels(),[])
        self.store.reserve(self.sid,283)
        self.store.stop(self.sid,'unknown','new timeout',283)
        self.assertEqual(self.store.unknown_channels(),[self.sid])
        report=AsyncMock()
        with patch.object(claude_channel,'emitted_channel',AsyncMock(return_value=self.current)) as read:
            self.assertTrue(await local.Watchers(self.store)._recover_emitted(self.store.subscription(self.sid),report))
        read.assert_awaited_once()
        report.assert_awaited_once_with(283,'started',None,'claude:'+self.current)
        self.assertEqual(self.rows()[0],old)

    async def test_historical_unknown_does_not_block_later_fetched_recovery(self):
        await self.apply()
        old=self.rows()[0]
        self.store.reserve(self.sid,283)
        self.store.stop(self.sid,'unknown','new timeout',283)
        value={'ok':True,'session_id':self.sid,'party':'local','status':'open','latest_seq':283,
               'messages':[{'seq':283,'message':'private','from_party':'member','fetched':True}]}
        client=AsyncMock()
        client.get.return_value=httpx.Response(200,json=value,request=httpx.Request('GET','https://example.invalid/poll'))
        with patch.object(local,'_Reporter',return_value=AsyncMock()):
            self.assertTrue(await local.Watchers(self.store)._recover_fetched(self.store.subscription(self.sid),client))
        self.assertEqual(self.rows()[0],old)
        self.assertEqual(self.rows()[1]['state'],'fetched')

    async def test_lock_contention_and_remote_failure_do_not_change_rows(self):
        old=self.rows()
        with patch.object(local,'_claim_session',return_value=None),patch.object(local,'_YIELD_SECONDS',0):
            with self.assertRaises(DeliveryError):await self.apply()
        async def fail(_):raise DeliveryError('CHANNEL_STATE_INVALID','read failed')
        self.public=fail
        with self.assertRaises(DeliveryError):await self.apply()
        self.assertEqual(self.rows(),old);self.assertEqual(self.rows('superseded_replies'),[])

    async def test_transaction_failure_rolls_back_marker(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_resume BEFORE UPDATE ON subscriptions BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):await self.apply()
        self.assertEqual(self.rows('superseded_replies'),[])
        self.assertEqual(self.store.subscription(self.sid)['state'],'unknown')
