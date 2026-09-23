"""Local shared-watch Source read fence after parent repository pause."""
import argparse

from verify_server_mapping import call


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if not args.after_restart:
        assert call("reset")[0] == 200
        assert call("repository-create")[0] == 200
        assert call("repository-seed-shared")[0] == 200
        status, visible = call("source-read", "GET")
        assert status == 200 and len(visible["items"]) == 1, visible
        assert call("repository-pause")[0] == 200
    status, hidden = call("source-read", "GET")
    assert status == 200 and hidden == {"items": []}, hidden
    status, state = call("repository-state", "GET")
    assert status == 200 and state["revision"] == 2, state
    assert state["watchProof"] == state["accessible"] == 1, state
    print("Paused parent hides shared-watch Source on local D1")


if __name__ == "__main__":
    main()
