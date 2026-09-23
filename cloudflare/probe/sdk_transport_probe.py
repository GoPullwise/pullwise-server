"""Local loopback transport checks; never contacts a model service."""
import json
import logging
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def child(mode):
    import httpx2
    from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient
    logging.getLogger("typesafe_sdk").disabled = True
    body = json.dumps({"model": "jev-1.13.0", "answers": {
        "q": {"type": "choice", "choice": "present", "probabilities": {"present": 1.0, "absent": 0.0}, "confidence": 1.0}
    }, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                if mode == "slow_headers":
                    time.sleep(0.3)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if mode == "slow_body":
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.025)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("ready", flush=True)
    started = time.monotonic()
    try:
        with httpx2.Client(timeout=0.1, trust_env=False) as http:
            with TypeSafeClient(api_key="local-fixture-not-a-credential",
                base_url=f"http://127.0.0.1:{server.server_port}", model="jev-1.13.0",
                retry=RetryPolicy(max_retries=0), timeout=0.1, http_client=http) as client:
                result = client.system_one(state="Local fixture", questions={
                    "q": Choice(instructions="Fixture only", criteria={"present": "Present", "absent": "Absent"})
                })
                outcome = result.model
    except Exception as error:
        outcome = type(error).__name__
    print(json.dumps({"mode": mode, "outcome": outcome, "elapsed": round(time.monotonic()-started, 3)}))


if __name__ == "__main__":
    if len(sys.argv) == 2:
        child(sys.argv[1])
    else:
        results = []
        for mode in ("normal", "slow_headers", "slow_body"):
            process = subprocess.Popen([sys.executable, "-u", __file__, mode],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            startup_guard = threading.Timer(15, process.kill)
            startup_guard.start()
            ready = process.stdout.readline().strip()
            startup_guard.cancel()
            if ready != "ready":
                process.kill()
                process.communicate()
                raise RuntimeError("Probe startup failed")
            try:
                output, _ = process.communicate(timeout=2)
                results.append(json.loads(output))
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
                results.append({"mode": mode, "outcome": "no_total_exit_within_2s", "processReaped": True})
        assert results[0]["outcome"] == "jev-1.13.0", results[0]
        assert results[1]["outcome"] == "TypeSafeAPITimeoutError", results[1]
        print(json.dumps({"runtime": "CPython loopback, not Workers", "modelCalls": 0, "results": results}, indent=2))
