"""Loopback-only real HTTP fixture for the sibling Web. Synthetic identity/data."""
import json
import sys
import threading
from unittest.mock import patch

from pullwise_server import app, db
from test_product_api_routes import ProductApiRoutesTest
from test_pr_thread_semantics import ThreadFixture


def main():
    fixture = ProductApiRoutesTest()
    fixture.setUp()
    cookie_policy = patch.object(app, "cookie_same_site", return_value="None")
    cookie_policy.start()
    fixture.addCleanup(cookie_policy.stop)
    server = None
    try:
        # Reuse the protected-route test account/session setup; no real credentials.
        user = app.USERS.pop("usr_1")
        user["id"] = "owner"
        app.USERS["owner"] = user
        app.SESSIONS["ses_1"]["userId"] = "owner"
        f = ThreadFixture(fixture.db_path)
        f.source("1", "Please add tests.")
        f.source("2", "Done. Why this approach?", reply="1")
        f.publish("1", "change_request")
        f.publish("2", "question", "completion_claim", dependencies=("1",))
        watch = f.store.create_watch(owner_id="owner", target_repository_id=None,
            upstream_repository_id="upstream", billing_owner_id="owner", interests=["docs"],
            enabled=True, analysis_enabled=False)
        release = f.store.upsert_source_snapshot(source_id="release-http", source_type="release",
            external_key="release:http", repository_id="upstream", content={"body": "Docs release"},
            source_facts={"releaseId": "1"}, source_url="https://github.com/a/b/releases/1",
            processing_mode="model", completeness="partial", lifecycle="active", observed_at=f.now)
        f.store.set_source_context(source_id=release["id"], context_id=watch["watchScopeKey"],
            watch_id=watch["id"], context_version=1, configuration_revision=1, authorization_revision=1,
            authorization_valid_until=f.now + 300, accessible=True, billing_owner_id="owner", analysis_enabled=False)
        key = app.API_KEY_PREFIX + "synthetic_http_contract"
        db.create_api_key(dict(id="key-http", user_id="owner", name="Synthetic contract",
            key_prefix=app.api_key_prefix(key), key_hash=app.api_key_hash(key),
            scopes=["items:read", "items:write", "sync:write"], restrictions={}))
        before = f.store.processing_usage(billing_owner_id="owner", period="period")
        server = app.PullwiseThreadingHTTPServer(("127.0.0.1", 0), app.PullwiseHandler)
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        print(json.dumps(dict(origin=f"http://127.0.0.1:{server.server_port}",
            cookie=f"{app.SESSION_COOKIE}=ses_1", key=key, watchId=watch["id"], releaseId=release["id"])), flush=True)
        if sys.stdin.readline().strip() == "verify":
            unchanged = before == f.store.processing_usage(billing_owner_id="owner", period="period")
            print(json.dumps(dict(unchangedUsage=unchanged, modelJobs=f.store.count_jobs(job_type="analyze_source"))), flush=True)
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == "__main__":
    main()
