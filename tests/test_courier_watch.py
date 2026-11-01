"""Finite checks for authenticated activity and one wake per outage."""
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

from call_bridge import courier_watch as watch, server
from call_bridge.auth import Authenticator
from call_bridge.db import CallStore


class CourierWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'calls.db'
        self.store = CallStore(self.path)
        self.env = mock.patch.dict(os.environ, {
            watch.URL_ENV: 'http://localhost/webhook', watch.KEY_ENV: 'private-key',
            'CALL_BRIDGE_GROKBOT_DELIVERY': 'courier',
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_initial_grace_exact_boundary_and_durable_one_attempt(self):
        self.store.start_courier_monitor(1000)
        self.assertIsNone(self.store.claim_courier_silence(999.999))
        self.assertEqual(self.store.claim_courier_silence(1000), {
            'event': 'courier_silent', 'last_seen': None,
        })
        restarted = CallStore(self.path)
        restarted.start_courier_monitor(9000)
        self.assertIsNone(restarted.claim_courier_silence(9000))
        restarted.record_courier_seen(10000)
        self.assertIsNone(restarted.claim_courier_silence(9999.999))
        payload = restarted.claim_courier_silence(10000)
        self.assertEqual(payload['last_seen'], '1970-01-01T02:46:40+00:00')
        self.assertIsNone(self.store.claim_courier_silence(20000))

    def test_only_successful_courier_requests_reset_timer_through_real_auth(self):
        tokens = Path(self.tmp.name) / 'tokens.json'
        tokens.write_text(json.dumps({'tokens': [
            {'name': 'courier', 'system': 'grokbot', 'courier': True,
             'sha256': hashlib.sha256(b'courier-token').hexdigest()},
            {'name': 'member', 'system': 'grokbot', 'id': 'g-1',
             'sha256': hashlib.sha256(b'member-token').hexdigest()},
        ]}))
        self.store.start_courier_monitor(1000)
        app = Starlette(routes=[
            Route('/v0/grokbot/deliveries/inflight', server.rest_inflight_deliveries),
            Route('/v0/grokbot/deliveries/claim', server.rest_claim_deliveries, methods=['POST']),
            Route('/v0/grokbot/directory', server.rest_directory_push, methods=['PUT']),
            Route('/v0/directory', server.rest_directory),
            Route('/health', server.health),
        ])
        app.add_middleware(server.BearerAuthMiddleware)
        with mock.patch.object(server, 'AUTH', Authenticator('', str(tokens))), \
             mock.patch.object(server, 'store', self.store), \
             mock.patch.object(watch, 'watch', new=mock.AsyncMock()), \
             mock.patch.object(watch, 'time', mock.Mock(time=mock.Mock(return_value=1200))), \
             mock.patch.object(server, 'store_directory_push', return_value={'ok': True}), \
             mock.patch.object(server, 'search_directory', return_value={'ok': True, 'members': []}):
            with TestClient(app) as client:
                courier = {'Authorization': 'Bearer courier-token'}
                member = {'Authorization': 'Bearer member-token'}
                for path, headers in [
                    ('/v0/grokbot/deliveries/inflight?courier_id=test', {}),
                    ('/v0/grokbot/deliveries/inflight?courier_id=test', member),
                    ('/v0/grokbot/deliveries/inflight', courier),
                    ('/v0/directory', courier),
                    ('/health', courier),
                ]:
                    client.get(path, headers=headers)
                self.assertIsNotNone(self.store.claim_courier_silence(1000))
                for method, path, kwargs in [
                    ('get', '/v0/grokbot/deliveries/inflight?courier_id=test', {}),
                    ('post', '/v0/grokbot/deliveries/claim', {'json': {'courier_id': 'test'}}),
                    ('put', '/v0/grokbot/directory', {'json': {'members': []}}),
                ]:
                    response = getattr(client, method)(path, headers=courier, **kwargs)
                    self.assertEqual(response.status_code, 200)
                    self.assertIsNone(self.store.claim_courier_silence(1199.999))
                    self.assertIsNotNone(self.store.claim_courier_silence(1200))

    def test_missing_configuration_and_marian_do_not_record_activity(self):
        for changes in [{watch.URL_ENV: ''}, {watch.KEY_ENV: ''}, {'CALL_BRIDGE_GROKBOT_DELIVERY': 'marian'}]:
            with self.subTest(changes=changes), mock.patch.dict(os.environ, changes):
                self.assertFalse(watch.enabled())
                with mock.patch.object(self.store, 'record_courier_seen') as record:
                    asyncio.run(watch.record_activity(self.store, mock.Mock(system='grokbot', courier=True),
                                                     '/v0/grokbot/directory', 200))
                    record.assert_not_called()

    def test_timer_fires_without_requests_and_does_not_retry_failure(self):
        async def exercise():
            ticks = iter([1000, 1299.999, 1300, 9000])
            count = 0
            async def sleep(_seconds):
                nonlocal count
                count += 1
                if count == 4:
                    raise asyncio.CancelledError
            with mock.patch.object(watch, 'time', mock.Mock(time=mock.Mock(side_effect=lambda: next(ticks)))), \
                 mock.patch.object(watch.asyncio, 'sleep', side_effect=sleep), \
                 mock.patch.object(watch, 'notify', side_effect=OSError('private-key')) as notify:
                with self.assertLogs(watch.log, level='ERROR') as logs:
                    with self.assertRaises(asyncio.CancelledError):
                        await watch.watch(self.store)
                self.assertNotIn('private-key', '\n'.join(logs.output))
                notify.assert_called_once_with({'event': 'courier_silent', 'last_seen': None})
        asyncio.run(exercise())
        self.assertIsNone(CallStore(self.path).claim_courier_silence(10000))

    def test_lifespan_starts_and_cancels_enabled_watcher(self):
        async def exercise():
            started = asyncio.Event()
            stopped = asyncio.Event()
            async def worker(_store):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
            with mock.patch.object(watch, 'watch', side_effect=worker):
                async with server._lifespan(server.mcp):
                    await asyncio.wait_for(started.wait(), 1)
                self.assertTrue(stopped.is_set())
            with mock.patch.dict(os.environ, {watch.KEY_ENV: ''}), \
                 mock.patch.object(watch, 'watch') as worker:
                async with server._lifespan(server.mcp):
                    pass
                worker.assert_not_called()
        asyncio.run(exercise())

    def test_real_server_arms_monitor_without_any_mcp_or_rest_requests(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        env = {**os.environ, 'CALL_BRIDGE_DB': str(self.path), 'CALL_BRIDGE_PORT': str(port),
               'CALL_BRIDGE_HOST': '127.0.0.1', 'CALL_BRIDGE_TOKEN': 'fake-server-token',
               'CALL_BRIDGE_TOKENS_FILE': '', 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        process = subprocess.Popen([sys.executable, '-m', 'call_bridge.server'], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while True:
                with sqlite3.connect(self.path) as db:
                    row = db.execute('SELECT last_seen FROM courier_monitor').fetchone()
                if row is not None:
                    self.assertIsNone(row[0])
                    break
                self.assertIsNone(process.poll(), 'real server exited before arming monitor')
                self.assertLess(time.monotonic(), deadline, 'monitor never armed without an incoming request')
                time.sleep(0.05)
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_post_bearer_payload_and_no_redirect_or_secret_logs(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            code = 204
            def log_message(self, *args):
                pass
            def do_POST(self):
                captured.append((self.path, self.headers.get('Authorization'),
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(self.code)
                self.send_header('Location', '/must-not-follow')
                self.end_headers()
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        payload = {'event': 'courier_silent', 'last_seen': None}
        url = f'http://127.0.0.1:{http.server_port}/secret-url'
        try:
            with mock.patch.dict(os.environ, {watch.URL_ENV: url}):
                for code in (204, 302, 401, 500):
                    Handler.code = code
                    with self.assertLogs(watch.log, level='INFO') as logs:
                        watch.notify(payload)
                    self.assertNotIn('private-key', '\n'.join(logs.output))
                    self.assertNotIn(url, '\n'.join(logs.output))
                self.assertEqual(captured, [('/secret-url', 'Bearer private-key', payload)] * 4)
                with mock.patch('urllib.request.OpenerDirector.open', side_effect=TimeoutError(url + 'private-key')):
                    with self.assertLogs(watch.log, level='ERROR') as logs:
                        watch.notify(payload)
                    self.assertNotIn('private-key', '\n'.join(logs.output))
                    self.assertNotIn(url, '\n'.join(logs.output))
        finally:
            http.shutdown()
            http.server_close()
            thread.join()
