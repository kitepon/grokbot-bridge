"""旧台帳の送信先欠測を、公的出力記録と管理者の期待値で結び直す。送信しない。"""
import argparse
import asyncio
import json
import os
import time
import uuid

from . import local, claude_channel
from .codex_delivery import DeliveryError, state_root


def refuse():
    raise DeliveryError('CHANNEL_BINDING_UNSAFE', '配送先を確証できません。変更していません')


def _is_uuid(name):
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False


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
        def channels():
            # 受け口はUUID名のdirectoryだけ。同じ置き場にある claude-sessions/ などの索引は受け口ではない。
            directory=state_root()/'steer-channels'
            return sorted(p.name for p in directory.iterdir() if p.is_dir() and _is_uuid(p.name))
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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['session','expected-current','expected-owner','source-channel']:
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--seq',type=int,required=True)
    parser.add_argument('--expected-after',type=int,required=True)
    args=parser.parse_args()
    try:
        result=asyncio.run(bind(args.session,args.seq,args.expected_current,args.expected_owner,args.source_channel,args.expected_after))
    except (DeliveryError,ValueError,OSError) as exc:
        parser.exit(1,f'{exc}\n')
    print(json.dumps(result))

if __name__ == '__main__': main()
