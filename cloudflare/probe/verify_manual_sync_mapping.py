"""Local trusted public-watch manual fact-sync D1 queue probe."""
import argparse

from verify_server_mapping import call


STATE = {"jobs": 1, "generation": 1, "state": "queued",
         "attempts": 0, "reserved": 1}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if not args.after_restart:
        assert call("reset")[0] == 200
        assert call("watch-create")[0] == 200
        status, first = call("manual-sync")
        assert status == 200 and first["jobType"] == "sync_watch"
        assert first["reused"] is False and first["generation"] == 1
    status, replay = call("manual-sync")
    assert status == 200 and replay["id"] == "job-manual-probe"
    assert replay["reused"] is True
    assert call("manual-sync-state", "GET") == (200, STATE)
    print("Manual fact-sync queue persisted without model attempts or new reservations")


if __name__ == "__main__":
    main()
