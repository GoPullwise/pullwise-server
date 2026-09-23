"""Candidate Server Worker entry; no probe endpoints or scheduled trigger."""
import json
import time
from urllib.parse import urlsplit

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
        }
        status, payload = await handle_http_request(
            method=request.method,
            path=urlsplit(request.url).path,
            headers=headers,
            read_body=read_body,
            binding=getattr(self.env, "DB", None),
            creem_secret=getattr(self.env, "PULLWISE_CREEM_WEBHOOK_SECRET", ""),
            configured_products=products,
            now=int(time.time()),
        )
        return Response.json(payload, status=status)
