import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from filenap.server import App, handler
from filenap.engine import Problem
from filenap.planner import Planner


class UnusedPlanner:
    model = "not-a-live-model-test"

    def make(self, *_):
        raise AssertionError("HTTP boundary tests must not call a model")


class LocalHTTPBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "note.txt").write_text("Only a test fixture.")
        self.app = App(self.root, UnusedPlanner())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.app, 0))
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = handler(self.app, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.pool.shutdown()
        self.app.engine.close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        all_headers = {"Content-Type": "application/json"}
        all_headers.update(headers or {})
        try:
            response = urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=all_headers), timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), dict(response.headers)

    def test_api_requires_local_session_header(self):
        status, _, _ = self.request("/api/state")
        self.assertEqual(status, 403)
        status, body, _ = self.request("/api/state", headers={"X-FileNap-Token": self.app.token})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["scan"]["files"]), 1)

    def test_foreign_origin_rejected_even_with_valid_session_header(self):
        status, _, _ = self.request("/api/apply", {"plan_id": "x", "ids": []},
                                    {"Origin": "https://other.example", "X-FileNap-Token": self.app.token})
        self.assertEqual(status, 403)
        self.assertEqual(self.app.engine.batches(), [])
        self.assertTrue((self.root / "note.txt").exists())

    def test_unexpected_host_rejected(self):
        status, _, _ = self.request("/", headers={"Host": "attacker.example:" + str(self.port)})
        self.assertEqual(status, 403)

    def test_unknown_or_malformed_saved_identifiers_fail_without_file_actions(self):
        for value in [None, {}, "not-a-plan", "0" * 32]:
            status, _, _ = self.request("/api/apply", {"plan_id": value, "ids": ["x"]},
                                        {"X-FileNap-Token": self.app.token})
            self.assertEqual(status, 409)
        self.assertEqual(self.app.engine.batches(), [])

    def test_static_routes_do_not_expose_state_or_arbitrary_files(self):
        for path in ["/.filenap/state.sqlite3", "/../engine.py", "/%2e%2e/server.py"]:
            status, _, _ = self.request(path)
            self.assertEqual(status, 404)
        status, _, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")


class ModelTransport(unittest.TestCase):
    def test_model_requests_do_not_follow_unconfigured_redirects(self):
        reached = []

        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                reached.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        destination = "http://127.0.0.1:" + str(sink.server_address[1]) + "/forwarded"

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", destination)
                self.send_header("Content-Length", "0")
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in [sink, redirect]]
        for thread in threads:
            thread.start()
        try:
            with self.assertRaises(Problem):
                Planner("http://127.0.0.1:" + str(redirect.server_address[1])).request("/api/tags")
            self.assertEqual(reached, [])
        finally:
            for server in [sink, redirect]:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()


if __name__ == "__main__":
    unittest.main()
