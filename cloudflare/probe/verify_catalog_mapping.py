"""Exercise only synthetic trusted public Billing catalog D1 publication."""
import argparse

from verify_server_mapping import call


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after-restart", action="store_true")
    args = parser.parse_args()
    if args.after_restart:
        assert call("billing-catalog-state", "GET") == (200,
            {"revision": 2, "amount": "30"})
        assert call("billing-catalog-stage-2") == (200, {"changed": False})
        print("Synthetic Billing catalog revision persisted after restart")
        return
    assert call("reset")[0] == 200
    assert call("billing-catalog-state", "GET") == (200,
        {"revision": 0, "amount": None})
    assert call("billing-catalog-stage-1") == (200, {"changed": True})
    assert call("billing-catalog-stage-2") == (200, {"changed": True})
    assert call("billing-catalog-stage-1")[0] == 409
    assert call("billing-catalog-state", "GET") == (200,
        {"revision": 2, "amount": "30"})
    print("Synthetic Billing catalog monotonic publication passed on local D1")


if __name__ == "__main__":
    main()
