"""Exercise actual authenticated stream HTTP connections using synthetic loopback only."""
import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from live_adapter import PilotController, handler_for  # noqa: E402
from stream_http import StreamHttpSender  # noqa: E402
from test_live_adapter import targets  # noqa: E402


class StreamHttpTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.connections = 0
        self.controller = PilotController(targets(), sender=lambda *_: True)

        class Video:
            def submit(_, message):
                self.messages.append(message)
                return True

            def snapshot(_):
                return {}

        base = handler_for(self.controller, 'synthetic-token', video_output=Video())

        class Handler(base):
            def setup(inner):
                super().setup()
                self.connections += 1

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.sender = StreamHttpSender(self.url, 'synthetic-token')
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.sender.close)

    def test_fresh_stream_messages_reuse_one_authenticated_connection(self):
        for sequence in range(12):
            self.sender.post({'sequence': sequence})
        self.assertEqual(self.messages, [{'sequence': i} for i in range(12)])
        self.assertEqual(self.connections, 1)

    def test_closed_connection_recovers_on_next_fresh_message(self):
        self.sender.post({'sequence': 1})
        self.sender.close()
        self.sender.post({'sequence': 2})
        self.assertEqual(self.messages, [{'sequence': 1}, {'sequence': 2}])
        self.assertEqual(self.connections, 2)

    def test_unauthorized_body_cannot_be_reused_as_a_subsequent_request(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=1)
        self.addCleanup(connection.close)
        connection.request('POST', '/stream', json.dumps({'sequence': 1}),
                           {'X-Adapter-Token': 'wrong', 'Connection': 'keep-alive'})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        self.assertTrue(response.will_close)
        response.read()
        self.assertEqual(self.messages, [])

    def test_non_stream_routes_keep_legacy_connection_close(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=1)
        self.addCleanup(connection.close)
        connection.request('GET', '/status', headers={
            'X-Adapter-Token': 'synthetic-token', 'Connection': 'keep-alive'})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.will_close)
        response.read()

    def test_reused_stream_connection_rechecks_auth_and_does_not_reuse_unread_body(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=1)
        self.addCleanup(connection.close)
        for token, expected in [('synthetic-token', 200), ('wrong', 403)]:
            connection.request('POST', '/stream', json.dumps({'sequence': 1}), {
                'X-Adapter-Token': token, 'Connection': 'keep-alive'})
            response = connection.getresponse()
            self.assertEqual(response.status, expected)
            self.assertEqual(response.will_close, expected == 403)
            response.read()
        self.assertEqual(len(self.messages), 1)
