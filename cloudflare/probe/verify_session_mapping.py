"""Exercise trusted synthetic session issue/revoke on local workerd/D1."""
import argparse

from verify_server_mapping import call


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if args.after_restart:
        assert call("session-state", "GET") == (200,
            {"count": 1, "syntheticPresent": True})
        assert call("oauth-state", "GET") == (200,
            {"count": 1, "syntheticPresent": True})
        assert call("oauth-consume") == (200,
            {"kind": "login", "redirectTo": "dashboard"})
        assert call("oauth-consume")[0] == 409
        assert call("session-revoke") == (200, {"revoked": True})
        assert call("session-state", "GET") == (200,
            {"count": 0, "syntheticPresent": False})
        assert call("oauth-state", "GET") == (200,
            {"count": 0, "syntheticPresent": False})
        print("Synthetic session mapping restart and revoke passed on local D1")
        return
    assert call("reset")[0] == 200
    status, session = call("session-issue")
    assert status == 200 and session["id"] == "ses-synthetic"
    assert call("session-issue")[0] == 409
    assert call("oauth-issue") == (200, {"issued": True})
    assert call("oauth-issue")[0] == 409
    assert call("session-state", "GET") == (200,
        {"count": 1, "syntheticPresent": True})
    print("Synthetic session mapping issue and duplicate fence passed on local D1")


if __name__ == "__main__":
    main()
