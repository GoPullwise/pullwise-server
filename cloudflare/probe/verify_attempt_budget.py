"""Local D1 two-scope monthly/rolling attempt admission with month boundary."""
import json
import sys
import uuid

from verify_local import call


def command(name, token=None):
    status, body = call("/attempt/" + name + ("?token=" + token if token else ""), "POST")
    return status, body


def state():
    status, body = call("/attempt/state")
    assert status == 200, body
    return body


def main():
    if "--after-restart" in sys.argv:
        before = state()
        assert len(before["attempts"]) == 3, before
        assert before["counters"]["global:2026-10"] == 1, before
        assert command("a", uuid.uuid4().hex)[0] == 409  # Rolling owner gate survives.
        assert state() == before
        assert command("advance61")[0] == 200
        assert command("a", uuid.uuid4().hex)[0] == 200
        print(json.dumps({"monthlyAndRollingRestart": "passed", "modelCalls": 0}))
        return
    assert command("reset")[0] == 200
    assert command("a", uuid.uuid4().hex)[0] == 200
    assert command("a", uuid.uuid4().hex)[0] == 409  # Owner rolling limit is one.
    assert command("b", uuid.uuid4().hex)[0] == 200
    assert command("advance30")[0] == 200  # Cross UTC month while inside rolling window.
    before = state()
    assert before["period"] == "2026-10", before
    assert command("a", uuid.uuid4().hex)[0] == 409
    assert state() == before  # Neither month's counter was half consumed.
    assert command("advance31")[0] == 200
    assert command("a", uuid.uuid4().hex)[0] == 200
    current = state()
    assert current["counters"]["global:2026-09"] == 2
    assert current["counters"]["global:2026-10"] == 1
    assert len(current["attempts"]) == 3
    print(json.dumps({"ownerGlobalMonthly": "passed", "rollingAcrossMonth": "passed",
                      "atomicRejection": "passed", "restartPending": True, "modelCalls": 0}))


if __name__ == "__main__":
    main()
