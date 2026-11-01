"""旧台帳の送信先欠測を、公的出力記録と管理者の期待値で結び直す。送信しない。"""
import argparse
import asyncio
import json
import os
import time
import uuid
import httpx

from . import local, claude_channel
from .codex_delivery import DeliveryError, state_root


def refuse():
    raise DeliveryError('CHANNEL_BINDING_UNSAFE', '配送先を確証できません。変更していません')


def _is_uuid(name):
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False


def channels():
    return sorted(p.name for p in (state_root()/'steer-channels').iterdir()
                  if p.is_dir() and _is_uuid(p.name))


async def claude_owners(ids):
    """Claude Code の受け口ごとの会話。Cursor/背景/Codex の受け口は対象外。読めない・形の崩れた受け口は拒否。"""
    owners={}
    for channel in ids:
        kind, session = await claude_channel.owner_kind(channel)
        if kind == 'claude':
            if session is None: refuse()
            owners[channel]=session
        elif kind not in ('cursor','background','codex'): refuse()
    return owners


async def bind(session_id, seq, expected_current, expected_owner, source_channel, expected_after):
    for value in [session_id, expected_current, expected_owner, source_channel]:
        uuid.UUID(value)
    store = local.store
    before = store.subscription(session_id)
    if (before['state'], before['harness'], before['delivery_mode'], before['thread_id'], before['after_seq'], before['hold_seq']) != ('unknown','claude-code','channel',expected_current,expected_after,None):
        refuse()
    with store.connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM deliveries WHERE session_id=? ORDER BY seq',(session_id,))]
    pending = [r for r in rows if r['state'] in ('unknown','sending')]
    if (len(pending) != 1 or pending[0]['seq'] != seq or pending[0]['channel_id'] is not None
            or pending[0]['delivery_id'] != str(uuid.uuid5(local._DELIVERY_NAMESPACE,f'{session_id}:{seq}'))):
        refuse()
    fd = None
    try:
        deadline = time.monotonic()+local._YIELD_SECONDS
        while fd is None:
            fd = local._claim_session(session_id)
            if fd is not None: break
            if time.monotonic() >= deadline: refuse()
            await asyncio.sleep(local._YIELD_POLL_SECONDS)
        ids=channels()
        owners=await claude_owners(ids)
        if owners.get(expected_current) != expected_owner or owners.get(source_channel) != expected_owner: refuse()
        states={}
        for channel, owner in owners.items():
            if owner == expected_owner:
                value=await claude_channel.run(['state','--channel',channel,'--delivery',pending[0]['delivery_id']])
                if 'state' not in value or value['state'] not in (None,'emitted','queued','sending','unknown','withdrawn'): refuse()
                states[channel]=value['state']
        if states.get(source_channel) != 'emitted' or any(state is not None for channel,state in states.items() if channel != source_channel): refuse()
        if channels() != ids or await claude_owners(ids) != owners: refuse()
        # 走査中のchannel/予約変更を拒否し、元state/error/atを保ったまま結合だけ確定する。
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sub=db.execute('SELECT * FROM subscriptions WHERE session_id=?',(session_id,)).fetchone()
            current=[dict(r) for r in db.execute('SELECT * FROM deliveries WHERE session_id=? ORDER BY seq',(session_id,))]
            if sub is None or dict(sub) != before or current != rows: refuse()
            db.execute('UPDATE deliveries SET channel_id=? WHERE session_id=? AND seq=?',(source_channel,session_id,seq))
        return {'session_id':session_id,'seq':seq,'bound_channel':source_channel,'input_sent':False}
    finally:
        if fd is not None: os.close(fd)


async def supersede(session_id, seq, expected_current, expected_owner, expected_after, expected_at, reason):
    """管理者が不要と決めた旧返信だけを失効する。元unknownを成功へ変えない。"""
    for value in [session_id, expected_current, expected_owner]:
        uuid.UUID(value)
    if (type(seq) is not int or seq != expected_after+1 or not 0 < expected_at < float('inf')
            or not reason.strip() or len(reason) > 500): refuse()
    store=local.store
    before=store.subscription(session_id)
    if (before['state'],before['harness'],before['delivery_mode'],before['thread_id'],before['after_seq'],before['hold_seq']) != ('unknown','claude-code','channel',expected_current,expected_after,None): refuse()
    with store.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM deliveries WHERE session_id=? ORDER BY seq',(session_id,))]
        old=[dict(r) for r in db.execute('SELECT * FROM superseded_replies WHERE session_id=? ORDER BY seq',(session_id,))]
    old_seqs={r['seq'] for r in old}
    pending=[r for r in rows if r['seq'] not in old_seqs and (r['seq'] > expected_after or r['state'] in ('unknown','sending')
             or (r['state']=='submitted' and not r['confirmed']))]
    if (len(pending)!=1 or pending[0]['seq']!=seq or pending[0]['state']!='unknown' or pending[0]['confirmed']!=1
            or pending[0]['error']!='DELIVERY_PREVIOUSLY_STARTED' or pending[0]['at']!=expected_at
            or pending[0]['channel_id']!=expected_current
            or pending[0]['delivery_id']!=str(uuid.uuid5(local._DELIVERY_NAMESPACE,f'{session_id}:{seq}'))
            or any(r['seq']==seq for r in old)): refuse()
    fd=None
    try:
        deadline=time.monotonic()+local._YIELD_SECONDS
        while fd is None:
            fd=local._claim_session(session_id)
            if fd is not None: break
            if time.monotonic()>=deadline: refuse()
            await asyncio.sleep(local._YIELD_POLL_SECONDS)
        async with httpx.AsyncClient(timeout=10) as client:
            response=await client.get(local._rest_url(session_id),headers=local._headers(),
                                      params={'party':'local','after_seq':expected_after,'peek':'1'})
            response.raise_for_status()
            value=response.json()
        if (not local._valid_poll_result(value,session_id,'local',expected_after)
                or value.get('ok') is not True or value.get('status')=='hungup'
                or not value['messages'] or value['messages'][0]['seq']!=seq
                or value['messages'][0].get('fetched') is not False): refuse()
        ids=channels()
        owners=await claude_owners(ids)
        if owners.get(expected_current)!=expected_owner: refuse()
        async def absent():
            for channel,owner in owners.items():
                if owner==expected_owner:
                    value=await claude_channel.run(['state','--channel',channel,'--delivery',pending[0]['delivery_id']])
                    if 'state' not in value or value['state'] is not None: refuse()
        await absent()
        if channels()!=ids or await claude_owners(ids)!=owners: refuse()
        await absent()
        if channels()!=ids or await claude_owners(ids)!=owners: refuse()
        # nullは過去未実行の証明ではない。明示的な失効だけを、元配送行を変えず記録する。
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sub=db.execute('SELECT * FROM subscriptions WHERE session_id=?',(session_id,)).fetchone()
            current=[dict(r) for r in db.execute('SELECT * FROM deliveries WHERE session_id=? ORDER BY seq',(session_id,))]
            superseded=[dict(r) for r in db.execute('SELECT * FROM superseded_replies WHERE session_id=? ORDER BY seq',(session_id,))]
            if sub is None or dict(sub)!=before or current!=rows or superseded!=old: refuse()
            db.execute('INSERT INTO superseded_replies(session_id,seq,reason,at) VALUES(?,?,?,?)',
                       (session_id,seq,reason.strip(),time.time()))
            db.execute("UPDATE subscriptions SET state='active',after_seq=?,last_error=NULL WHERE session_id=?",(seq,session_id))
        return {'session_id':session_id,'superseded_seq':seq,'after_seq':seq,
                'original_outcome':'unknown','input_sent':False,'delivery_success_asserted':False}
    finally:
        if fd is not None: os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['session','expected-current','expected-owner']:
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--source-channel')
    parser.add_argument('--supersede',action='store_true')
    parser.add_argument('--expected-at',type=float)
    parser.add_argument('--reason')
    parser.add_argument('--seq',type=int,required=True)
    parser.add_argument('--expected-after',type=int,required=True)
    args=parser.parse_args()
    if args.supersede:
        if args.source_channel or args.expected_at is None or args.reason is None: parser.error('supersede requires --expected-at and --reason, without --source-channel')
    elif not args.source_channel or args.expected_at is not None or args.reason is not None:
        parser.error('binding requires --source-channel, without --expected-at or --reason')
    try:
        if args.supersede:
            result=asyncio.run(supersede(args.session,args.seq,args.expected_current,args.expected_owner,args.expected_after,args.expected_at,args.reason))
        else:
            result=asyncio.run(bind(args.session,args.seq,args.expected_current,args.expected_owner,args.source_channel,args.expected_after))
    except (DeliveryError,ValueError,OSError,httpx.HTTPError) as exc:
        parser.exit(1,f'{exc}\n')
    print(json.dumps(result))

if __name__ == '__main__': main()
