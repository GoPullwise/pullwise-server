"""Candidate Server Worker entry; no probe endpoints or scheduled trigger."""
import json
import time
from urllib.parse import urlsplit, parse_qs

from workers import Response, WorkerEntrypoint

from pullwise_server.cloudflare_http_contract import handle_http_request


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        raw_products = getattr(self.env, "PULLWISE_CREEM_PRODUCT_IDS_JSON", "")
        try:
            products = json.loads(raw_products) if raw_products else None
        except (TypeError, ValueError):
            products = None

        async def read_body():
            body = await request.bytes()
            return body if isinstance(body, bytes) else body.to_bytes()

        headers = {
            "Content-Length": request.headers.get("content-length") or "",
            "creem-signature": request.headers.get("creem-signature") or "",
            "Cookie": request.headers.get("cookie") or "",
            "Authorization": request.headers.get("authorization") or "",
            "X-Pullwise-Api-Key": request.headers.get("x-pullwise-api-key") or "",
            "X-Request-Id": request.headers.get("x-request-id") or "",
            "If-Match": request.headers.get("if-match") or "",
            "Origin": request.headers.get("origin") or "",
            "Referer": request.headers.get("referer") or "",
        }
        path = urlsplit(request.url).path
        status, payload = await handle_http_request(
            method=request.method,
            path=path,
            params=parse_qs(urlsplit(request.url).query),
            headers=headers,
            read_body=read_body,
            binding=getattr(self.env, "DB", None),
            creem_secret=getattr(self.env, "PULLWISE_CREEM_WEBHOOK_SECRET", ""),
            configured_products=products,
            now=int(time.time()),
            cookie_same_site=getattr(self.env, "PULLWISE_COOKIE_SAME_SITE", "Lax"),
            trusted_origins={value.strip() for value in (
                getattr(self.env, "PULLWISE_ALLOWED_ORIGINS", "") + "," +
                getattr(self.env, "PULLWISE_APP_URL", "")).split(",")
                if value.strip() and value.strip() != "*"},
        )
        if status == 204:
            return Response(None, status=status)
        response_headers = None
        if (status == 200 and isinstance(payload, dict)
                and type(payload.get("revision")) is int
                and (path.startswith("/api/v1/items/")
                     or path.startswith("/api/v1/watches/"))):
            response_headers = {"ETag": f'"{payload["revision"]}"'}
        return Response.json(payload, status=status, headers=response_headers)
