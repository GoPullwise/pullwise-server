"""Exercise synthetic trusted RepositoryService and shared-watch D1 mapping."""
import argparse

from verify_server_mapping import call


FINAL = {"revision": 2, "jobState": "cancelled", "chargeState": "released",
         "reserved": 0, "config": 2, "analysis": 0, "accessible": 0,
         "watchProof": 0, "attempts": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if args.after_restart:
        status, state = call("repository-state", "GET")
        assert status == 200 and state == FINAL, state
        assert call("repository-change-installation")[0] == 409
        print("Repository and shared-watch fences persisted after local workerd restart")
        return
    assert call("reset")[0] == 200
    status, service = call("repository-create")
    assert status == 200 and service["revision"] == 1, service
    assert call("repository-seed-shared")[0] == 200
    status, before = call("repository-state", "GET")
    assert status == 200 and before["jobState"] == "queued" and before["reserved"] == 1, before
    status, changed = call("repository-change-installation")
    assert status == 200 and changed["revision"] == 2, changed
    status, state = call("repository-state", "GET")
    assert status == 200 and state == FINAL, state
    print("Repository and shared-watch cancellation passed on local workerd/D1")


if __name__ == "__main__":
    main()
