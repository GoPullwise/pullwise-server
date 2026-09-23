"""Exercise only the local candidate Worker with synthetic webhook bytes."""
import hashlib
import hmac
import json
import urllib.error
import urllib.request


BASE = "http://127.0.0.1:8797"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RAW = (b'{"id":"evt-http-worker","eventType":"subscription.canceled",'
       b'"object":{"id":"sub_fixture","metadata":{"userId":"owner"}}}')


def call(path, *, raw=None, signature=None, extra_headers=None):
    headers = dict(extra_headers or {})
    if raw is not None:
        headers.update({"Content-Length": str(len(raw)),
                        "Content-Type": "application/json",
                        "creem-signature": signature or ""})
    request = urllib.request.Request(BASE + path, data=raw, headers=headers,
                                     method="POST" if raw is not None else "GET")
    try:
        with OPENER.open(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        return response.code, json.load(response)


def main():
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
    status, restricted_watches = call("/api/v1/watches", extra_headers={
        "Authorization": "Bearer pwk_local_http_test"})
    assert status == 200 and len(restricted_watches["items"]) == 1
    assert call("/api/v1/me", extra_headers={
        "Cookie": "pw_session=session-local",
        "Authorization": "Bearer pwk_local_http_test"})[0] == 400
    assert call("/api/v1/items")[0] == 404
    assert call("/webhooks/creem", raw=RAW, signature="bad")[0] == 400
    signature = hmac.new(b"synthetic-secret", RAW, hashlib.sha256).hexdigest()
    assert call("/webhooks/creem", raw=RAW, signature=signature) == (200, {"received": True})
    assert call("/webhooks/creem", raw=RAW, signature=signature) == (200, {"received": True})
    print("Local Server Worker auth, usage, signature and replay passed")


if __name__ == "__main__":
    main()
