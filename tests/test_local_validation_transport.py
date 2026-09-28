"""A loopback validation request must never leave through a system proxy."""
import importlib.util
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def test_local_pause_check_bypasses_proxy_and_is_finite(monkeypatch):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            body = b'{"error":{"code":"D1_ACCESS_PAUSED"}}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(key, "http://127.0.0.1:1")
        for key in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(key, "")
        path = Path(__file__).resolve().parents[1] / "scripts/check-worker-cost-pause.py"
        spec = importlib.util.spec_from_file_location("pause_transport_check", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(sys, "argv", [str(path), "--origin", f"http://127.0.0.1:{server.server_port}"])
        module.main()
        assert paths == ["/health", "/api/v1/me", "/api/v1/expenses", "/auth/github/authorize"]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
