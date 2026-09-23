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
    if "--watch-only" in sys.argv:
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
        assert status == 200 and updated["revision"] == watch["revision"] + 1
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
