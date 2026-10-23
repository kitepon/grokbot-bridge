"""Check Docker image contracts using only temporary data and synthetic callers.

Usage: python scripts/check_image_candidate.py BASELINE_IMAGE CANDIDATE_IMAGE
The images must already exist locally. This does not build, pull or deploy them.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


def docker(*arguments: str, **kwargs):
    return subprocess.run(["docker", *arguments], check=True, text=True,
                          capture_output=True, timeout=60, **kwargs)


CHECK = r'''
import asyncio, json, os, urllib.request, urllib.error
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
base='http://127.0.0.1:18910'
token=os.environ['CALL_BRIDGE_TOKEN']
phase=int(os.environ['CHECK_PHASE'])
sid=os.environ.get('CHECK_SESSION')
def request(path, body=None, auth=token):
    headers={'Authorization':'Bearer '+auth} if auth else {}
    data=json.dumps(body).encode() if body is not None else None
    if data is not None: headers['Content-Type']='application/json'
    with urllib.request.urlopen(urllib.request.Request(base+path, data=data, headers=headers),timeout=5) as r:
        return json.load(r)
assert request('/health')['ok']
for auth in ['', 'synthetic-wrong']:
    try: request('/v0/directory',auth=auth)
    except urllib.error.HTTPError as e: assert e.code == 401
    else: raise AssertionError('Unauthenticated request accepted')
assert request('/v0/directory')['ok']
if phase == 1:
    opened=request('/v0/sessions',{'local_id':'synthetic-caller','local_label':'fixture','member_name':'fixture'})
    sid=opened['session_id']
history=request('/v0/sessions/'+sid+'/history')
assert history['latest_seq'] == phase-1, history
sent=request('/v0/sessions/'+sid+'/messages',{'from_party':'member','message':'phase '+str(phase),'reply_required':False})
assert sent['seq'] == phase and sent['delivery']['status']=='stored', sent
request('/v0/sessions/'+sid+'/receipts',{'seq':phase,'state':'fetched'})
history=request('/v0/sessions/'+sid+'/history')
assert [m['message'] for m in history['messages']]==['phase '+str(i) for i in range(1,phase+1)]
async def check_mcp():
    async with streamablehttp_client(base+'/mcp',headers={'Authorization':'Bearer '+token}) as (read,write,_):
        async with ClientSession(read,write) as session:
            await session.initialize()
            result=await session.call_tool('call_info',{'session_id':sid})
            assert not result.isError
            info=result.structuredContent
            assert info['status']=='open'
            assert len(info['local_delivery'])==phase
            assert all(r['state']=='fetched' for r in info['local_delivery'])
asyncio.run(check_mcp())
print(json.dumps({'session_id':sid,'phase':phase,'latest_seq':phase,'receipts':phase,'health':200,'auth':[401,401,200],'mcp':True}))
'''


def main() -> None:
    old, new = sys.argv[1:]
    identities = {}
    for image in (old, new):
        metadata = json.loads(docker("image", "inspect", image).stdout)[0]
        identities[image] = {"id": metadata["Id"], "arch": metadata["Architecture"],
                             "user": metadata["Config"].get("User", ""),
                             "revision": metadata["Config"]["Labels"]["org.opencontainers.image.revision"]}
    sid = None
    phases = []
    with tempfile.TemporaryDirectory(prefix="bridge-candidate-", dir=Path.cwd()) as temporary:
        root = Path(temporary)
        data = root / "data"
        data.mkdir()
        directory = root / "directory.json"
        directory.write_text(json.dumps({"members": [{"id": "fixture-member", "name": "fixture"}]}))
        for phase, image in enumerate((old, new, old, new), 1):
            name = "bridge-fixture-" + uuid.uuid4().hex[:12]
            docker("run", "--detach", "--name", name, "--network", "none", "--read-only",
                   "--tmpfs", "/tmp:rw,nosuid,nodev", "--mount", f"type=bind,src={data},dst=/app/data",
                   "--mount", f"type=bind,src={directory},dst=/fixture/directory.json,readonly",
                   "-e", "CALL_BRIDGE_TOKEN=synthetic-candidate-token",
                   "-e", "CALL_BRIDGE_DB=/app/data/calls.db", "-e", "CALL_BRIDGE_HOST=0.0.0.0",
                   "-e", "CALL_BRIDGE_DIRECTORY=/fixture/directory.json",
                   "-e", "CALL_BRIDGE_AGENTS_ROOT=/fixture/absent-agents", image)
            try:
                for _ in range(60):
                    probe = subprocess.run(["docker", "exec", name, "python", "-c",
                                            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18910/health',timeout=1)"],
                                           capture_output=True, timeout=5)
                    if probe.returncode == 0:
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError("Candidate did not become ready")
                result = docker("exec", "-e", f"CHECK_PHASE={phase}", "-e", f"CHECK_SESSION={sid or ''}",
                                name, "python", "-B", "-c", CHECK)
                outcome = json.loads(result.stdout)
                sid = outcome["session_id"]
                if phase == 2:
                    docker("restart", "--time", "10", name)
                    check_restart = "from call_bridge.db import CallStore; s=CallStore('/app/data/calls.db'); assert s.history(" + repr(sid) + ")[\"latest_seq\"]==2"
                    docker("exec", name, "python", "-B", "-c", check_restart)
                    outcome["restart_keeps_latest_db"] = True
                outcome["runtime"] = json.loads(docker("exec", name, "python", "-c",
                                                      "import json,os; print(json.dumps({'uid':os.getuid(),'gid':os.getgid()}))").stdout)
                docker("stop", "--time", "10", name)
                state = json.loads(docker("inspect", name).stdout)[0]["State"]
                assert state["ExitCode"] == 0, state
                outcome["sigterm_exit"] = state["ExitCode"]
                outcome["session_id"] = "synthetic"
                phases.append(outcome)
            finally:
                docker("rm", "--force", name)
    print(json.dumps({"images": identities, "phases": phases, "bellteam_required": False,
                      "network": "none", "read_only_root": True,
                      "writable": ["/app/data (SQLite DB/WAL/SHM)", "/tmp (tmpfs)"],
                      "production_data_or_credentials": False}, indent=2))


if __name__ == "__main__":
    main()
