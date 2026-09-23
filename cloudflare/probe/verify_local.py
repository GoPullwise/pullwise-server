"""Deterministic loopback-only acceptance checks for the CF1 probe."""
import concurrent.futures
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8794"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def call(path, method="GET"):
    request = urllib.request.Request(BASE + path, method=method)
    try:
        with HTTP.open(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body.startswith("{") else body
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        return error.code, json.loads(body) if body.startswith("{") else body

def state():
    status, payload = call("/state")
    assert status == 200, payload
    return {row["scope"]: row["used"] for row in payload["results"]}

def main():
    if "--after-restart" in sys.argv:
        current = state()
        assert current["global"] == 1 and current["a"] + current["b"] == 1, current
        status, clock = call("/clock")
        assert status == 200 and clock["results"][0]["ticks"] >= 1, clock
        print(json.dumps({"restartPersistence": "passed", "budget": current, "clock": clock["results"]}))
        return
    status, imports = call("/")
    assert status == 200 and all(value == "ok" for value in imports["imports"].values()), imports
    assert call("/reset", "POST")[0] == 200
    assert call("/rollback", "POST") == (200, {"rollback": True})
    assert state() == {"a": 0, "b": 0, "global": 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        raced = list(executor.map(lambda owner: call("/admit/" + owner, "POST"), ["a", "b"]))
    assert sorted(status for status, _ in raced) == [200, 409], raced
    current = state()
    assert current["global"] == 1 and current["a"] + current["b"] == 1, current
    assert call("/admit/a", "POST")[0] == 409
    assert call("/admit/b", "POST")[0] == 409
    assert state() == current  # Zero-row CAS rolls back all subsequent writes.
    scheduled_status, scheduled_body = call("/cdn-cgi/local/scheduled?format=json")
    assert scheduled_status == 200, scheduled_body
    status, clock = call("/clock")
    assert status == 200 and clock["results"][0]["ticks"] >= 1, clock
    print(json.dumps({"runtime": imports["python"], "imports": imports["imports"],
        "rollback": "passed", "concurrentOwnerGlobalAdmission": "passed",
        "zeroRowRollback": "passed", "scheduled": "passed", "budget": current,
        "modelCalls": 0}, indent=2))

if __name__ == "__main__":
    main()
