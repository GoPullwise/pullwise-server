"""Local trusted repository fact-sync D1 queue and proof probe."""
import argparse

from verify_server_mapping import call


STATE = {"jobs": 1, "generation": 1, "state": "queued",
         "attempts": 0, "reserved": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if not args.after_restart:
        assert call("reset")[0] == 200
        assert call("repository-create")[0] == 200
        assert call("manual-sync-repository")[0] == 409
        assert call("repository-seed-manual-proof") == (200, {"staged": True})
        status, first = call("manual-sync-repository")
        assert status == 200 and first["jobType"] == "sync_repository"
        assert first["generation"] == 1 and first["reused"] is False
    status, replay = call("manual-sync-repository")
    assert status == 200 and replay["id"] == "job-repository-probe"
    assert replay["reused"] is True
    assert call("manual-sync-repository-state", "GET") == (200, STATE)
    print("Repository fact-sync queue persisted without model attempts or reservations")


if __name__ == "__main__":
    main()
