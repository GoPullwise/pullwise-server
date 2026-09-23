"""Local workerd/D1 lease, fencing, retry and publication protocol checks."""
import concurrent.futures
import json
import socket
import sys
import time
import uuid

from verify_local import call


def command(name, token=None):
    return call("/domain/" + name + ("?token=" + token if token else ""), "POST")


def state():
    status, result = call("/domain/state")
    assert status == 200, result
    return result["results"][0]


def main():
    if "--after-restart" in sys.argv:
        assert state()["state"] == "retry_wait"
        token = uuid.uuid4().hex
        assert command("claim", token)[0] == 409
        assert command("advance")[0] == 200
        assert command("claim", token)[0] == 200
        assert command("publish", token)[0] == 200
        assert state()["used"] == state()["results"] == 1
        print(json.dumps({"domainRetryRestart": "passed", "modelCalls": 0}))
        return
    assert command("reset")[0] == 200
    tokens = [uuid.uuid4().hex, uuid.uuid4().hex]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        raced = list(pool.map(lambda token: command("claim", token), tokens))
    assert sorted(status for status, _ in raced) == [200, 409], raced
    old = state()["token"]
    assert command("advance")[0] == 200
    assert command("advance")[0] == 200
    new = uuid.uuid4().hex
    assert command("claim", new)[0] == 200
    assert command("publish", old)[0] == 409
    assert state()["results"] == state()["used"] == 0
    assert command("publish", new)[0] == 200
    # Discard the response, then replay as if the response had been lost.
    assert command("publish", new)[0] == 409
    assert state()["results"] == state()["used"] == 1
    assert state()["reserved"] == 0
    assert command("reset")[0] == 200
    token = uuid.uuid4().hex
    assert command("claim", token)[0] == 200
    with socket.create_connection(("127.0.0.1", 8794), timeout=2) as connection:
        request = (f"POST /domain/publish?token={token} HTTP/1.1\r\nHost: 127.0.0.1:8794\r\n"
                   "Content-Length: 0\r\nConnection: close\r\n\r\n")
        connection.sendall(request.encode("ascii"))
        # The client loses the response after sending the request. Server may
        # have committed or cancelled; retry must settle exactly once either way.
    for _ in range(50):
        if state()["state"] == "succeeded":
            break
        time.sleep(0.02)
    replay_status, _ = command("publish", token)
    assert replay_status in {200, 409}
    assert state()["results"] == state()["used"] == 1
    assert state()["reserved"] == 0
    assert command("reset")[0] == 200
    token = uuid.uuid4().hex
    assert command("claim", token)[0] == 200
    assert command("break-reservation")[0] == 200
    assert command("publish", token)[0] == 409
    assert state()["state"] == "running"  # Late batch failure rolls back earlier job/result writes.
    assert state()["results"] == state()["used"] == 0
    for fence in ("revoke", "configure"):
        assert command("reset")[0] == 200
        token = uuid.uuid4().hex
        assert command("claim", token)[0] == 200
        assert command(fence)[0] == 200
        assert command("publish", token)[0] == 409
        assert state()["results"] == state()["used"] == state()["reserved"] == 0
    assert command("reset")[0] == 200
    token = uuid.uuid4().hex
    assert command("claim", token)[0] == 200
    assert command("retry", token)[0] == 200
    assert command("claim", uuid.uuid4().hex)[0] == 409
    assert state()["state"] == "retry_wait"  # Leave for real restart check.
    print(json.dumps({"claimRace": "passed", "expiredExecutor": "passed", "fencing": "passed",
                      "atomicPublicationUsage": "passed", "responseReplay": "passed",
                      "clientDisconnectRetry": "passed",
                      "retryBeforeDeadline": "passed", "restartPending": True, "modelCalls": 0}))


if __name__ == "__main__":
    main()
