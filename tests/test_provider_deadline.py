from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from model_client import ModelClient, ProviderError, Settings


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                slow = request['messages'][0]['content'] == 'slow'
                body = b'{"choices":[{"message":{"content":"fixture answer"}}]}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    if slow:
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(.04)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Expected when the client cancels a synthetic slow response.

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.client = ModelClient(Settings(f'http://127.0.0.1:{self.server.server_port}/v1', 'fixture', 'fixture', workers=2))
        self.addCleanup(self.client.close)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_deadline_cancels_continuously_trickling_response(self):
        started = time.monotonic()
        with self.assertRaises(ProviderError):
            self.client.generate([{'role': 'user', 'content': 'slow'}], started + .3)
        self.assertLess(time.monotonic() - started, 1.2)

    def test_cancellation_does_not_break_parallel_or_following_requests(self):
        def slow():
            with self.assertRaises(ProviderError):
                self.client.generate([{'role': 'user', 'content': 'slow'}], time.monotonic() + .3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(slow)
            self.assertEqual(self.client.generate([{'role': 'user', 'content': 'fast'}], time.monotonic() + 3), 'fixture answer')
            pending.result()
        self.assertEqual(self.client.generate([{'role': 'user', 'content': 'fast'}], time.monotonic() + 3), 'fixture answer')
