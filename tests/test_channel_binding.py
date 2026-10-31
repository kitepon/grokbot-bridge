import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from call_bridge import local, claude_channel
from call_bridge.codex_delivery import DeliveryError
SID='4d141f30-b918-4b63-9667-5f5c827c2e65'

class BindingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=local.LocalStore(Path(self.tmp.name));self.store.add(SID,'old',Path('.'),'member',delivery_mode='channel',harness='claude-code')
        self.store.reserve(SID,224);self.store.stop(SID,'unknown','original timeout',224)
        self.store.adopt_channel(SID,'new',None)
        self.watcher=local.Watchers(self.store)
    async def test_emitted_original_is_reconciled_without_resend_or_error_loss(self):
        before=self.store.status(SID)['deliveries'][0];before['at']=self.store.reserved_at(SID,224)
        report=AsyncMock()
        with patch.object(claude_channel,'emitted_channel',AsyncMock(return_value='old')) as check:
            self.assertTrue(await self.watcher._recover_emitted(self.store.subscription(SID),report))
        self.assertEqual(check.await_args.args[2],'old')
        row=self.store.status(SID)['deliveries'][0]
        self.assertEqual((row['state'],row['error'],self.store.reserved_at(SID,224)),('submitted',before['error'],before['at']))
        self.assertEqual(self.store.subscription(SID)['after_seq'],0)
        report.assert_awaited_once_with(224,'started',None,'claude:old')
        send=AsyncMock()
        with patch.object(local,'reply_text',return_value='body'),patch.object(claude_channel,'send',send):
            self.assertEqual(await self.watcher._handle(self.store.subscription(SID),{'seq':224,'message':'old'},224,report),'next')
        send.assert_not_awaited()
    async def test_unknown_or_failed_read_does_not_change_state(self):
        for result in [None,DeliveryError('READ_FAILED','no')]:
            before=self.store.status(SID)
            with patch.object(claude_channel,'emitted_channel',AsyncMock(side_effect=result if isinstance(result,Exception) else None,return_value=result)):
                self.assertFalse(await self.watcher._recover_emitted(self.store.subscription(SID),AsyncMock()))
            self.assertEqual(before,self.store.status(SID))
    async def test_rebinding_during_read_refuses_reconciliation(self):
        async def changed(*args):
            self.store.adopt_channel(SID,'third',None);return 'old'
        with patch.object(claude_channel,'emitted_channel',changed):
            self.assertFalse(await self.watcher._recover_emitted(self.store.subscription(SID),AsyncMock()))
        self.assertEqual(self.store.status(SID)['deliveries'][0]['state'],'unknown')

class LegacyTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_binding_is_not_guessed(self):
        with patch.object(claude_channel,'delivery_state',AsyncMock()) as read:
            self.assertIsNone(await claude_channel.emitted_channel('new','delivery',None))
        read.assert_not_awaited()

class LegacyBindTest(unittest.IsolatedAsyncioTestCase):
    """実Steerの置き場: UUID名の受け口と、受け口ではない claude-sessions/ 索引が同じ steer-channels/ に並ぶ。"""
    def setUp(self):
        import uuid
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,CALL_BRIDGE_STATE=self.tmp.name);env.start();self.addCleanup(env.stop)
        root=Path(self.tmp.name);self.store=local.LocalStore(root)
        self.current,self.source,self.owner,self.cursor,self.other=[str(uuid.uuid4()) for _ in range(5)]
        for key in [self.current,self.source,self.cursor,self.other,'claude-sessions',self.owner.upper()]: (root/'steer-channels'/key).mkdir(parents=True)
        (root/'steer-channels'/'claude-sessions'/self.owner).mkdir()
        self.store.add(SID,self.current,Path('.'),'member',delivery_mode='channel',harness='claude-code')
        self.store.reserve(SID,224);self.store.stop(SID,'unknown','original timeout',224)
        with self.store.connect() as db: db.execute('UPDATE deliveries SET channel_id=NULL')
        # 本人のclaude受け口2つ、ownerの無いCursor受け口、別人のclaude受け口。
        self.channels={self.current:('claude',self.owner),self.source:('claude',self.owner),self.cursor:('cursor',None),self.other:('claude','someone')}
        self.states={self.source:'emitted'}
    def rows(self):
        with self.store.connect() as db: return [dict(r) for r in db.execute('SELECT * FROM deliveries')]
    async def run_cli(self,args,**_):
        if args[0]=='owner':
            kind,session=self.channels[args[2]]
            if kind=='unreadable': raise DeliveryError('CHANNEL_STATE_INVALID','no')
            return {'kind':kind,'session':session}
        self.assertEqual(args[0],'state');self.assertEqual(self.channels[args[2]][0],'claude')
        value=self.states.get(args[2],None)
        return {} if value=='missing' else {'state':value}
    async def bind(self):
        from call_bridge import channel_recover
        with patch.object(local,'store',self.store),patch.object(claude_channel,'run',self.run_cli):
            return await channel_recover.bind(SID,224,self.current,self.owner,self.source,0)
    async def assert_refused(self):
        before=self.rows()[0]
        with self.assertRaises(DeliveryError): await self.bind()
        self.assertEqual(self.rows()[0],before);self.assertEqual(self.store.subscription(SID)['state'],'unknown')
    async def test_real_layout_binds_only_the_unique_emitted_source(self):
        before=self.rows()[0]
        self.assertEqual(await self.bind(),{'session_id':SID,'seq':224,'bound_channel':self.source,'input_sent':False})
        after=self.rows()[0];self.assertEqual(after.pop('channel_id'),self.source);before.pop('channel_id')
        self.assertEqual(after,before);self.assertEqual(self.store.subscription(SID)['state'],'unknown')
    async def test_other_delivery_state_on_same_owner_refuses(self):
        for other_state in ['queued','sending','unknown','emitted','invalid','missing']:
            self.states[self.current]=other_state
            await self.assert_refused()
    async def test_unreadable_or_malformed_channel_refuses(self):
        for channel,entry in [(self.cursor,('unreadable',None)),(self.cursor,('claude',None)),(self.cursor,(None,None)),(self.cursor,('mystery',None))]:
            self.channels[channel]=entry
            await self.assert_refused()
    async def test_owner_mismatch_refuses(self):
        self.channels[self.current]=('claude','someone')
        await self.assert_refused()
