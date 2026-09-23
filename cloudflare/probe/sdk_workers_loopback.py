"""Synthetic fixed-SDK network check through the local Python Worker."""
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeProvider(BaseHTTPRequestHandler):
    mode = "normal"
    disconnect_event = threading.Event()
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps({"model": "jev-1.13.0", "answers": {
            "q": {"type": "choice", "choice": "present", "probabilities": {"present": 1.0, "absent": 0.0}, "confidence": 1.0}
        }, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        if self.mode == "large_body":
            body += b" " * (2 * 1024 * 1024)
        if self.mode == "slow_headers":
            time.sleep(0.3)
        if self.mode == "slow_header_stream":
            try:
                self.connection.sendall(b"HTTP/1.1 200 OK\r\n")
                for _ in range(120):
                    self.connection.sendall(b"X-Probe: ok\r\n")
                    time.sleep(0.025)
                wire = (b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
                self.connection.sendall(wire)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.mode == "slow_body":
            for byte in body:
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.disconnect_event.set()
                    break
                time.sleep(0.025)
        elif self.mode == "large_body":
            self.wfile.write(body)
        else:
            self.wfile.write(body)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    assert mode in {"normal", "slow_headers", "slow_header_stream", "slow_body", "large_body"}
    FakeProvider.mode = mode
    server = ThreadingHTTPServer(("127.0.0.1", 8795), FakeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    request = urllib.request.Request("http://127.0.0.1:8794/sdk-network", method="POST")
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=8) as response:
        result = json.load(response)
    assert result["modelCalls"] == 0, result
    if mode == "normal":
        assert result["outcome"] == "jev-1.13.0", result
    else:
        assert result["outcome"] != "jev-1.13.0" and result["elapsed"] < 2.5, result
        if mode == "slow_body":
            assert FakeProvider.disconnect_event.wait(1), "slow body socket stayed open after SDK exit"
        FakeProvider.mode = "normal"
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=8) as response:
            recovered = json.load(response)
        assert recovered["outcome"] == "jev-1.13.0" and recovered["elapsed"] < 1, recovered
    print(json.dumps({"mode": mode, "workersSdkLoopback": "passed", "result": result}))
