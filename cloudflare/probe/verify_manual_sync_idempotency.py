"""Local D1 atomic Job and idempotency response probe."""
import argparse

from verify_server_mapping import call


STATE = {"jobs": 1, "receipts": 2, "attempts": 0, "reserved": 1}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if not args.after_restart:
        assert call("reset")[0] == 200
        assert call("watch-create")[0] == 200
        status, first = call("manual-sync-idempotent")
        assert status == 200 and first["id"] == "job-idem-probe"
        assert first["requestId"] == "req-first"
        assert call("manual-sync-idempotent") == (200, first)
        status, second = call("manual-sync-idempotent-second-key")
        assert status == 200 and second["id"] == first["id"]
        assert second["requestId"] == "req-second"
    else:
        status, replay = call("manual-sync-idempotent")
        assert status == 200 and replay["id"] == "job-idem-probe"
        assert replay["requestId"] == "req-first"
    assert call("manual-sync-idempotent-state", "GET") == (200, STATE)
    print("Atomic manual sync Job and idempotency responses persisted on local D1")


if __name__ == "__main__":
    main()
