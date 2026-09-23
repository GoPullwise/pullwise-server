"""Public httpx2 transport experiment for a total decoded-response bound."""
import time

import httpx2


class BoundedTransport(httpx2.BaseTransport):
    def __init__(self, *, deadline_seconds: float, max_bytes: int):
        self.deadline_seconds = deadline_seconds
        self.max_bytes = max_bytes
        self.inner = httpx2.HTTPTransport(trust_env=False, retries=0)

    def handle_request(self, request):
        started = time.monotonic()
        response = self.inner.handle_request(request)
        chunks = []
        size = 0
        try:
            for chunk in response.iter_bytes():
                if time.monotonic() - started >= self.deadline_seconds:
                    raise httpx2.ReadTimeout("total response deadline", request=request)
                size += len(chunk)
                if size > self.max_bytes:
                    raise httpx2.ReadError("decoded response exceeds limit", request=request)
                chunks.append(chunk)
            if time.monotonic() - started >= self.deadline_seconds:
                raise httpx2.ReadTimeout("total response deadline", request=request)
            return httpx2.Response(response.status_code, headers=response.headers,
                                   content=b"".join(chunks), request=request)
        finally:
            response.close()

    def close(self):
        self.inner.close()
