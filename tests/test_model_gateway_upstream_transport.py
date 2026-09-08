from __future__ import annotations

import json
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from pullwise_server.model_gateway_streaming import RequestCancellation
from pullwise_server.model_gateway_upstream_transport import open_upstream
from tests.test_model_gateway_end_to_end import write_loopback_certificate


class UpstreamTransportTest(unittest.TestCase):
    def test_cancellation_closes_a_real_tls_stream_while_waiting_for_data(self):
        disconnected = threading.Event()
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.flush()
                try:
                    if not self.connection.recv(1):
                        disconnected.set()
                except OSError:
                    disconnected.set()
            def log_message(self, *_args):
                pass
        with tempfile.TemporaryDirectory() as temporary:
            certificate, key = write_loopback_certificate(Path(temporary))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                cancel = RequestCancellation()
                with patch.dict('os.environ', {'REQUESTS_CA_BUNDLE': str(certificate)}):
                    response = open_upstream(f'https://localhost:{server.server_port}/completion',
                        headers={'Authorization':'Bearer local-test'}, payload={'messages': ['测试']},
                        timeout_seconds=5, cancellation=cancel)
                received = []
                reader = threading.Thread(target=lambda: received.extend(response.iter_bytes(1024)))
                reader.start()
                time.sleep(0.05)
                self.assertTrue(reader.is_alive())
                cancel.cancel()
                reader.join(2)
                self.assertFalse(reader.is_alive(), 'cancel did not interrupt the blocked TLS read')
                self.assertTrue(disconnected.wait(2), 'upstream did not observe connection closure')
                self.assertEqual(received, [])
                self.assertEqual(requests, [{'messages': ['测试']}])
                response.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
