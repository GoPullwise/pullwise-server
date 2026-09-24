"""Exercise only the local candidate Worker with synthetic webhook bytes."""
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request


BASE = "http://127.0.0.1:8797"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RAW = (b'{"id":"evt-http-worker","eventType":"subscription.canceled",'
       b'"object":{"id":"sub_fixture","metadata":{"userId":"owner"}}}')


def call(path, *, raw=None, signature=None, extra_headers=None, method=None,
         return_headers=False):
    headers = dict(extra_headers or {})
    if raw is not None:
        headers.update({"Content-Length": str(len(raw)),
                        "Content-Type": "application/json",
                        "creem-signature": signature or ""})
    request = urllib.request.Request(BASE + path, data=raw, headers=headers,
                                     method=method or ("POST" if raw is not None else "GET"))
    try:
        with OPENER.open(request, timeout=30) as response:
            body = response.read()
            try:
                payload = json.loads(body) if body else {}
            except ValueError:
                payload = {"unexpectedBody": body[:200].decode("utf-8", "replace")}
            return (response.status, payload, dict(response.headers)) if return_headers else (response.status, payload)
    except urllib.error.HTTPError as response:
        body = response.read()
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {"unexpectedBody": body[:200].decode("utf-8", "replace")}
        return (response.code, payload, dict(response.headers)) if return_headers else (response.code, payload)


def main():
    same_site_none = "--same-site-none" in sys.argv
    if "--repository-list-only" in sys.argv:
        cookie = {"Cookie": "pw_session=session-local"}
        status, listing, headers = call("/api/v1/repositories",
            extra_headers=cookie, return_headers=True)
        assert status == 200 and [row["id"] for row in listing["items"]] == ["repo", "repo-two"]
        assert listing["items"][0]["service"]["repositoryId"] == "repo"
        assert listing["items"][1]["service"] is None
        assert next((value for key, value in headers.items()
            if key.lower() == "cache-control"), None) == "no-store"
        status, scoped = call("/api/v1/repositories", extra_headers={
            "Authorization": "Bearer pwk_local_http_test"})
        assert status == 200 and [row["id"] for row in scoped["items"]] == ["repo-two"]
        assert call("/api/v1/repositories")[0] == 401
        print("Local Server Worker complete repository directory passed")
        return
    if "--private-headers-only" in sys.argv:
        status, _, headers = call("/api/v1/me",
            extra_headers={"Cookie": "pw_session=session-local"},
            return_headers=True)
        assert status == 200
        assert next((value for key, value in headers.items()
            if key.lower() == "cache-control"), None) == "no-store"
        status, listing = call("/api/v1/watches",
            extra_headers={"Cookie": "pw_session=session-local"})
        assert status == 200 and listing["items"]
        status, _, headers = call("/api/v1/watches/" + listing["items"][0]["id"],
            extra_headers={"Cookie": "pw_session=session-local"},
            return_headers=True)
        assert status == 200
        assert next((value for key, value in headers.items()
            if key.lower() == "cache-control"), None) == "no-store"
        assert next((value for key, value in headers.items()
            if key.lower() == "etag"), None)
        print("Private product responses are no-store and detail keeps ETag")
        return
    if "--catalog-only" in sys.argv:
        status, public = call("/billing/plan")
        assert status == 200 and public["enabled"] is False
        assert [plan["id"] for plan in public["plans"]] == ["free", "pro", "max"]
        assert "account" not in public
        status, account = call("/billing/plan", extra_headers={
            "Cookie": "pw_session=session-local"})
        assert status == 200 and account["account"]["plan"] == "pro"
        assert account["plans"][1]["entitlements"]["monthlyProcessingLimit"] == 5000
        print("Local Server Worker public/Cookie Billing catalog read passed")
        return
    if "--key-create-only" in sys.argv:
        status, before = call("/api-keys", extra_headers={
            "Cookie": "pw_session=session-local"})
        assert status == 200 and [row["id"] for row in before["items"]] == ["key-local"]
        if "--after-restart" in sys.argv:
            print("Local Server Worker API-key one-time token stayed absent after restart")
            return
        body = b'{"name":"Synthetic HTTP","scopes":["profile:read"]}'
        headers = {"Cookie": "pw_session=session-local",
            "Origin": "http://127.0.0.1:5173"}
        assert call("/api-keys", raw=body, extra_headers={**headers,
            "Origin": "https://evil.example"})[0] == 403
        status, created, response_headers = call("/api-keys", raw=body,
            extra_headers=headers, return_headers=True)
        assert status == 201 and created["key"].startswith("pwk_")
        assert next((value for key, value in response_headers.items()
            if key.lower() == "cache-control"), None) == "no-store"
        status, listed = call("/api-keys", extra_headers={
            "Cookie": "pw_session=session-local"})
        assert status == 200 and any(row["id"] == created["id"] for row in listed["items"])
        assert created["key"] not in json.dumps(listed)
        assert call("/api/v1/me", extra_headers={
            "Authorization": "Bearer " + created["key"]})[0] == 200
        assert call("/api-keys/" + created["id"], method="DELETE",
            extra_headers=headers)[0] == 200
        assert call("/api/v1/me", extra_headers={
            "Authorization": "Bearer " + created["key"]})[0] == 401
        print("Local Server Worker API-key issue/use/revoke passed")
        return
    if "--key-delete-only" in sys.argv:
        status, keys = call("/api-keys", extra_headers={
            "Cookie": "pw_session=session-local"})
        assert status == 200
        if "--after-restart" in sys.argv:
            assert keys["items"] == []
            assert call("/api-keys/key-local", method="DELETE",
                extra_headers={"Cookie": "pw_session=session-local",
                    "Origin": "http://127.0.0.1:5173"})[0] == 404
            print("Local Server Worker API-key revocation persisted after restart")
            return
        assert [entry["id"] for entry in keys["items"]] == ["key-local"]
        assert call("/api-keys/key-local", method="DELETE",
            extra_headers={"Cookie": "pw_session=session-local",
                "Origin": "https://evil.example"})[0] == 403
        assert call("/api-keys/key-local", method="DELETE",
            extra_headers={"Cookie": "pw_session=session-local",
                "Origin": "http://127.0.0.1:5173"}) == (
                    200, {"ok": True, "id": "key-local", "revoked": True})
        assert call("/api-keys", extra_headers={
            "Cookie": "pw_session=session-local"})[1]["items"] == []
        assert call("/api/v1/me", extra_headers={
            "Authorization": "Bearer pwk_local_http_test"})[0] == 401
        print("Local Server Worker API-key revocation and Origin guard passed")
        return
    if "--watch-only" in sys.argv:
        status, public_plan = call("/billing/plan")
        assert status == 200 and public_plan["enabled"] is False
        assert [plan["id"] for plan in public_plan["plans"]] == ["free", "pro", "max"]
        assert public_plan["plans"][1]["entitlements"]["monthlyProcessingLimit"] == 5000
        assert "account" not in public_plan
        status, personal_plan = call("/billing/plan", extra_headers={
            "Cookie": "pw_session=session-local"})
        assert status == 200 and personal_plan["account"]["plan"] == "pro"
        status, billing, billing_headers = call("/billing", extra_headers={
            "Cookie": "pw_session=session-local"}, return_headers=True)
        assert status == 200 and billing["account"]["plan"] == "pro"
        assert billing["account"]["entitlements"]["monthlyProcessingLimit"] == 5000
        assert len(billing["account"]["processingActivity"]) == 2
        assert "reviewLimit" not in billing["account"] and "quotaActivity" not in billing["account"]
        assert next((value for key, value in billing_headers.items()
            if key.lower() == "cache-control"), None) == "no-store"
        assert call("/billing", extra_headers={
            "Authorization": "Bearer pwk_local_http_test"})[0] == 401
        status, keys, key_headers = call("/api-keys", extra_headers={
            "Cookie": "pw_session=session-local"}, return_headers=True)
        assert status == 200 and len(keys["items"]) == 1
        assert keys["items"] == keys["apiKeys"]
        assert "key_hash" not in json.dumps(keys) and "pwk_local_http_test" not in json.dumps(keys)
        assert next((value for key, value in key_headers.items()
            if key.lower() == "cache-control"), None) == "no-store"
        assert call("/api-keys", extra_headers={
            "Authorization": "Bearer pwk_local_http_test"})[0] == 401
        status, listing = call("/api/v1/watches", extra_headers={
            "Cookie": "pw_session=session-local"})
        assert status == 200 and listing["items"]
        status, restricted = call("/api/v1/watches", extra_headers={
            "Authorization": "Bearer pwk_local_http_test"})
        assert status == 200
        watch = restricted["items"][0] if restricted["items"] else listing["items"][0]
        status, before_usage = call("/api/v1/usage", extra_headers={
            "Cookie": "pw_session=session-local"})
        assert status == 200
        status, history = call("/api/v1/usage/events?module=updates&limit=1",
            extra_headers={"Cookie": "pw_session=session-local"})
        assert status == 200 and [row["id"] for row in history["items"]] == ["res-historical-2"]
        assert history["hasMore"] and history["nextCursor"]
        status, next_page = call("/api/v1/usage/events?module=updates&limit=1&cursor=" +
            history["nextCursor"], extra_headers={"Cookie": "pw_session=session-local"})
        assert status == 200 and [row["id"] for row in next_page["items"]] == ["res-historical"]
        assert next_page["hasMore"] is False
        assert call("/api/v1/usage/events?module=pr&cursor=" + history["nextCursor"],
            extra_headers={"Cookie": "pw_session=session-local"})[0] == 422
        body = b'{"analysisEnabled":true}'
        headers = {"Cookie": "pw_session=session-local",
            "Origin": "http://127.0.0.1:5173",
            "If-Match": str(watch["revision"])}
        if same_site_none:
            assert call("/api/v1/watches/" + watch["id"], raw=body,
                method="PATCH", extra_headers={**headers,
                    "Origin": "https://evil.example"})[0] == 403
        status, updated, response_headers = call("/api/v1/watches/" + watch["id"],
            raw=body, method="PATCH", extra_headers=headers,
            return_headers=True)
        assert status == 200 and updated["revision"] == watch["revision"] + 1, (status, updated)
        assert next((value for key, value in response_headers.items()
            if key.lower() == "etag"), None) == f'"{updated["revision"]}"'
        assert call("/api/v1/watches/" + watch["id"], raw=body,
            method="PATCH", extra_headers=headers)[0] == 412
        if "--delete-watch" in sys.argv:
            delete_headers = {**headers, "If-Match": str(updated["revision"])}
            if same_site_none:
                assert call("/api/v1/watches/" + watch["id"], method="DELETE",
                    extra_headers={**delete_headers,
                        "Origin": "https://evil.example"})[0] == 403
            assert call("/api/v1/watches/" + watch["id"], method="DELETE",
                extra_headers=delete_headers) == (204, {})
            assert call("/api/v1/watches/" + watch["id"],
                extra_headers={"Cookie": "pw_session=session-local"})[0] == 404
            status, after_usage = call("/api/v1/usage", extra_headers={
                "Cookie": "pw_session=session-local"})
            assert status == 200
            assert after_usage["usage"]["reserved"] == 0
            if restricted["items"]:
                assert before_usage["usage"]["reserved"] == 1
                assert call("/api/v1/jobs/sync-local", extra_headers={
                    "Cookie": "pw_session=session-local"})[0] == 404
        print("Local Server Worker watch PATCH and stale revision passed")
        return
    assert call("/health") == (200, {"ok": True, "service": "pullwise-server",
        "database": {"type": "d1", "configured": True}})
    assert call("/api/v1/me", extra_headers={
        "Cookie": "pw_session=session-local"})[1]["id"] == "owner"
    status, usage = call("/api/v1/usage", extra_headers={
        "Authorization": "Bearer pwk_local_http_test"})
    assert status == 200 and usage["service"] == "github_followups"
    assert usage["usage"]["metric"] == "intelligent_processing"
    status, watches = call("/api/v1/watches", extra_headers={
        "Cookie": "pw_session=session-local"})
    assert status == 200 and len(watches["items"]) == 2
    status, watch_detail, watch_headers = call("/api/v1/watches/" + watches["items"][0]["id"],
        extra_headers={"Cookie": "pw_session=session-local"}, return_headers=True)
    assert status == 200 and watch_detail["id"] == watches["items"][0]["id"]
    assert next((value for key, value in watch_headers.items() if key.lower() == "etag"), None) == f'"{watch_detail["revision"]}"'
    watch_patch = b'{"analysisEnabled":true}'
    status, changed_watch, changed_headers = call("/api/v1/watches/" + watch_detail["id"],
        raw=watch_patch, method="PATCH", return_headers=True,
        extra_headers={"Cookie": "pw_session=session-local",
            "Origin": "http://127.0.0.1:5173", "If-Match": str(watch_detail["revision"])})
    assert status == 200 and changed_watch["revision"] == watch_detail["revision"] + 1
    assert changed_watch["contextVersion"] == watch_detail["contextVersion"]
    assert next((value for key, value in changed_headers.items() if key.lower() == "etag"), None) == f'"{changed_watch["revision"]}"'
    assert call("/api/v1/watches/" + watch_detail["id"], raw=watch_patch,
        method="PATCH", extra_headers={"Cookie": "pw_session=session-local",
            "Origin": "http://127.0.0.1:5173",
            "If-Match": str(watch_detail["revision"])})[0] == 412
    status, restricted_watches = call("/api/v1/watches", extra_headers={
        "Authorization": "Bearer pwk_local_http_test"})
    assert status == 200 and len(restricted_watches["items"]) == 1
    assert call("/api/v1/watches/" + restricted_watches["items"][0]["id"],
        extra_headers={"Authorization": "Bearer pwk_local_http_test"})[0] == 200
    status, sources = call("/api/v1/sources?module=pr", extra_headers={
        "Cookie": "pw_session=session-local"})
    assert status == 200 and len(sources["items"]) == 2
    status, detail = call("/api/v1/sources/1", extra_headers={
        "Cookie": "pw_session=session-local"})
    assert status == 200 and detail["id"] == "1" and "content" in detail
    status, restricted_sources = call("/api/v1/sources", extra_headers={
        "Authorization": "Bearer pwk_local_http_test"})
    assert status == 200 and [item["id"] for item in restricted_sources["items"]] == ["1"]
    assert call("/api/v1/sources?updateSignal=security_fix_stated", extra_headers={
        "Cookie": "pw_session=session-local"})[0] == 422
    status, items = call("/api/v1/items?view=all", extra_headers={
        "Cookie": "pw_session=session-local"})
    assert status == 200 and len(items["items"]) == 1
    status, overview = call("/api/v1/items/overview", extra_headers={
        "Cookie": "pw_session=session-local"})
    assert status == 200 and overview["totalCount"] == 1
    assert overview["sourceCoverage"]["total"] == 2
    status, item, item_headers = call("/api/v1/items/" + items["items"][0]["id"], extra_headers={
        "Cookie": "pw_session=session-local"}, return_headers=True)
    assert status == 200 and item["id"] == items["items"][0]["id"]
    assert next((value for key, value in item_headers.items() if key.lower() == "etag"), None) == f'"{item["revision"]}"'
    assert isinstance(item["handlingHistory"], list)
    patch_body = json.dumps({"itemVersion": item["itemVersion"],
                             "disposition": "done"}).encode()
    if same_site_none:
        assert call("/api/v1/items/" + item["id"], raw=patch_body,
            method="PATCH", extra_headers={"Cookie": "pw_session=session-local",
                "Origin": "https://evil.example", "If-Match": str(item["revision"])})[0] == 403
    status, handled = call("/api/v1/items/" + item["id"], raw=patch_body,
        method="PATCH", extra_headers={"Cookie": "pw_session=session-local",
            "Origin": "http://127.0.0.1:5173",
            "If-Match": str(item["revision"])})
    assert status == 200 and handled["handling"]["disposition"] == "done"
    assert handled["revision"] == item["revision"] + 1
    assert call("/api/v1/items/" + item["id"], raw=patch_body,
        method="PATCH", extra_headers={"Cookie": "pw_session=session-local",
            "Origin": "http://127.0.0.1:5173",
            "If-Match": str(item["revision"])})[0] == 412
    status, restricted_items = call("/api/v1/items", extra_headers={
        "Authorization": "Bearer pwk_local_http_test"})
    assert status == 200 and restricted_items["items"] == []
    status, sync_job = call("/api/v1/jobs/sync-local", extra_headers={
        "Cookie": "pw_session=session-local"})
    assert status == 200 and sync_job["operation"] == "sync_watch"
    assert sync_job["status"] == "queued"
    assert call("/api/v1/me", extra_headers={
        "Cookie": "pw_session=session-local",
        "Authorization": "Bearer pwk_local_http_test"})[0] == 400
    assert call("/api/v1/repositories")[0] == 404
    assert call("/webhooks/creem", raw=RAW, signature="bad")[0] == 400
    signature = hmac.new(b"synthetic-secret", RAW, hashlib.sha256).hexdigest()
    assert call("/webhooks/creem", raw=RAW, signature=signature) == (200, {"received": True})
    assert call("/webhooks/creem", raw=RAW, signature=signature) == (200, {"received": True})
    print("Local Server Worker auth, usage, signature and replay passed")


if __name__ == "__main__":
    main()
