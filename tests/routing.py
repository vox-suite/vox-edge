"""Exercise the production Caddyfile with real HTTP mock upstreams.

Run: CADDY_BIN=/path/to/caddy python3 tests/routing.py
No provider credentials or production services are used.
"""
import contextlib
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.do_POST()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.received.append((self.command, self.path, body))
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')
            self.end_headers()
            self.wfile.flush()
            self.wfile.write(self.rfile.read(4))
            self.wfile.flush()
            return
        payload = json.dumps({'service': self.server.label, 'method': self.command,
                              'path': self.path, 'body': body.decode()}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


class Routing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resources = contextlib.ExitStack()
        cls.addClassCleanup(cls.resources.close)
        cls.upstreams = []
        for label in ('core', 'bridge'):
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
            server.label, server.received = label, []
            cls.resources.callback(server.server_close)
            cls.resources.callback(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.upstreams.append(server)
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            cls.port = reserve.getsockname()[1]
        env = dict(os.environ, PORT=str(cls.port),
                   CORE_API_UPSTREAM=f'127.0.0.1:{cls.upstreams[0].server_port}',
                   BRIDGE_UPSTREAM=f'127.0.0.1:{cls.upstreams[1].server_port}')
        binary = os.environ.get('CADDY_BIN', 'caddy')
        command = [binary, '--config', str(ROOT / 'Caddyfile'), '--adapter', 'caddyfile']
        subprocess.run([command[0], 'validate', *command[1:]], env=env, check=True)
        cls.logs = cls.resources.enter_context(tempfile.TemporaryFile(mode='w+'))
        cls.process = subprocess.Popen([command[0], 'run', *command[1:]], env=env,
                                       stdout=cls.logs, stderr=cls.logs)
        cls.resources.callback(cls.stop)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                break
            try:
                if cls.request('/health')[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        cls.logs.seek(0)
        raise RuntimeError('Caddy failed to start: ' + cls.logs.read())

    @classmethod
    def stop(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    @classmethod
    def request(cls, path, method='GET', body=None):
        connection = http.client.HTTPConnection('127.0.0.1', cls.port, timeout=3)
        try:
            connection.request(method, path, body, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_routing_preserves_namespace_method_query_and_body(self):
        for path, label in [('/v1/tasks/query?cursor=opaque', 'core'),
                            ('/bridge/twilio/voice?signature=test', 'bridge')]:
            with self.subTest(path=path):
                status, body = self.request(path, 'POST', '{"limit":10}')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {'service': label, 'method': 'POST',
                                                   'path': path, 'body': '{"limit":10}'})

    def test_liveness_private_probes_and_unknown_routes_never_reach_upstreams(self):
        before = [len(s.received) for s in self.upstreams]
        self.assertEqual(self.request('/health'), (200, b'ok'))
        for path in ('/health/live', '/health/ready', '/internal/v1/actions',
                     '/v1', '/v10/tasks', '/bridge', '/bridges/wa', '/unknown'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        for path in ('/.env', '/.env.production', '/.git/config', '/wp-login',
                     '/shell.php', '/admin/users', '/v1/shell.php'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 403)
        self.assertEqual([len(s.received) for s in self.upstreams], before)

    def test_websocket_upgrades_and_bidirectional_bytes(self):
        for path, upstream in [('/v1/tasks/id/socket', self.upstreams[0]),
                               ('/bridge/twilio/voice/stream', self.upstreams[1])]:
            with self.subTest(path=path), socket.create_connection(('127.0.0.1', self.port), 3) as sock:
                sock.sendall((f'GET {path} HTTP/1.1\r\nHost: localhost\r\n'
                              'Connection: Upgrade\r\nUpgrade: websocket\r\n'
                              'Sec-WebSocket-Version: 13\r\n'
                              'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n').encode())
                header = b''
                while not header.endswith(b'\r\n\r\n'):
                    header += sock.recv(1)
                    self.assertLess(len(header), 8192)
                self.assertIn(b'101 Switching Protocols', header)
                # After a protocol upgrade Caddy tunnels bytes, without interpreting frames.
                sock.sendall(b'ping')
                received = b''
                while len(received) < 4:
                    received += sock.recv(4 - len(received))
                self.assertEqual(received, b'ping')
                self.assertEqual(upstream.received[-1][:2], ('GET', path))


if __name__ == '__main__':
    unittest.main(verbosity=2)
