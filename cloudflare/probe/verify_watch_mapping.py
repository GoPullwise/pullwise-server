"""Exercise only the synthetic public-watch D1 command on local workerd."""
import argparse

from verify_server_mapping import call


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if args.after_restart:
        status, state = call("watch-state", "GET")
        assert status == 200 and state["active"] == 1, state
        assert state["contextVersion"] == 1 and state["attempts"] == 0, state
        assert call("watch-create")[0] == 409
        print("Public watch mapping restart and replay passed on local D1")
        return
    assert call("reset")[0] == 200
    status, watch = call("watch-create")
    assert status == 200 and watch["contextVersion"] == 1, watch
    assert watch["watchScopeKey"] and watch["analysisEnabled"] is False, watch
    assert call("watch-create")[0] == 409
    assert call("watch-archive-queued")[0] == 200
    status, cancelled = call("watch-state", "GET")
    assert status == 200 and cancelled["active"] == 0, cancelled
    assert cancelled["reserved"] == 0 and cancelled["chargeState"] == "released", cancelled
    assert cancelled["jobState"] == "cancelled" and cancelled["firstAccessible"] == 0, cancelled
    assert call("reset")[0] == 200
    assert call("watch-create")[0] == 200
    assert call("claim")[0] == 200
    assert call("publish")[0] == 200
    status, current = call("source-read", "GET")
    assert status == 200 and current["items"][0]["contexts"][0]["assessments"]
    assert call("disable-analysis-secondary")[0] == 200
    status, historical = call("source-read", "GET")
    assert status == 200 and historical["items"][0]["contexts"][0]["assessments"]
    assert call("archive-secondary-watch")[0] == 200
    status, stale = call("source-read", "GET")
    assert status == 200 and stale["items"][0]["contexts"][0]["assessments"] == []
    assert call("reset")[0] == 200
    assert call("watch-create")[0] == 200
    status, state = call("watch-state", "GET")
    assert status == 200 and state["active"] == 1, state
    assert state["attempts"] == 0, state
    print("Public watch mapping creation and replay passed on local D1")


if __name__ == "__main__":
    main()
