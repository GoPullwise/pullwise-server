"""Local D1 test driver. No SQL/fixture upload or external-provider requests."""
import argparse
import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor


def call(path, method="POST"):
    request = urllib.request.Request("http://127.0.0.1:8796/server-map/" + path, method=method,
                                     data=b"{}" if method == "POST" else None)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        return response.code, json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if args.after_restart:
        status, state = call("state", "GET")
        assert status == 200 and state["used"] == 1 and state["attempts"] == 1 and state["results"] == 1, state
        assert call("publish")[0] == 409
        print("Server mapping restart/replay passed on local D1")
        return
    assert call("reset")[0] == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: call("claim")[0], range(2)))
    assert sorted(statuses) == [200, 409], statuses
    assert call("publish")[0] == 200
    assert call("publish")[0] == 409
    for fault in ("edit-parent", "change-account", "break-reservation"):
        assert call("reset")[0] == 200
        assert call("claim")[0] == 200
        assert call(fault)[0] == 200
        assert call("publish")[0] == 409
        state = call("state", "GET")[1]
        assert state["results"] == 0 and state["versions"] == 0 and state["used"] == 0, state
        assert state["jobState"] == "running" and state["paymentFactsPreserved"] == 1, state
    assert call("reset")[0] == 200
    assert call("change-account")[0] == 200
    assert call("claim")[0] == 409
    state = call("state", "GET")[1]
    assert state["attempts"] == 0 and state["jobState"] == "queued", state
    assert call("reset")[0] == 200
    assert call("claim")[0] == 200
    assert call("publish")[0] == 200
    print("Actual Server schema: combined claim/budget, multi-source/account fencing and atomic publication passed on local D1")


if __name__ == "__main__":
    main()
