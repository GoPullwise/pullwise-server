from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

import requests

from .model_gateway_runtime import GatewayRoute
from .model_gateway_endpoints import OFFICIAL_PROVIDER_ORIGINS
from .model_gateway_streaming import RequestCancellation
from .model_gateway_upstream_transport import open_upstream, UpstreamTransportError


MAX_UPSTREAM_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_UPSTREAM_STREAM_BYTES = 64 * 1024 * 1024


class UpstreamAdapterError(RuntimeError):
    pass


class OpenAIConnectionValidator:
    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not 1 <= float(timeout_seconds) <= 60:
            raise ValueError("validation timeout is invalid")
        self._timeout_seconds = float(timeout_seconds)

    def validate(self, *, endpoint_origin: str, adapter: str, provider: str, secret: bytes) -> list[str]:
        return self._probe(endpoint_origin=endpoint_origin, adapter=adapter, provider=provider, secret=secret)

    def canary(self, *, endpoint_origin: str, adapter: str, provider: str, secret: bytes) -> None:
        self._probe(endpoint_origin=endpoint_origin, adapter=adapter, provider=provider, secret=secret)

    def _probe(self, *, endpoint_origin: str, adapter: str, provider: str, secret: bytes) -> list[str]:
        if adapter != "openai-completions" or provider not in OFFICIAL_PROVIDER_ORIGINS:
            raise UpstreamAdapterError("provider adapter is unsupported")
        parsed = urlsplit(endpoint_origin)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise UpstreamAdapterError("provider endpoint origin is invalid")
        try:
            credential = secret.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UpstreamAdapterError("upstream credential is invalid") from exc
        origin = urlunsplit(("https", parsed.netloc.lower(), "", "", ""))
        if origin not in OFFICIAL_PROVIDER_ORIGINS[provider]:
            raise UpstreamAdapterError("provider endpoint origin is not allowlisted")
        try:
            models_path = "/models" if provider == "deepseek" else "/v1/models"
            response = requests.get(
                f"{origin}{models_path}",
                headers={"Authorization": f"Bearer {credential}"},
                timeout=self._timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise UpstreamAdapterError("provider validation failed") from exc
        try:
            if response.status_code < 200 or response.status_code >= 300:
                raise UpstreamAdapterError("provider validation failed")
            size = 0
            chunks: list[bytes] = []
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > 1024 * 1024:
                    raise UpstreamAdapterError("provider validation response is too large")
                chunks.append(chunk)
        finally:
            response.close()
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
            records = payload["data"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise UpstreamAdapterError("provider model catalog is invalid") from exc
        if not isinstance(payload, dict) or payload.get("object") != "list" or not isinstance(records, list) or not 1 <= len(records) <= 512:
            raise UpstreamAdapterError("provider model catalog is invalid")
        model_ids = []
        for record in records:
            model_id = record.get("id") if isinstance(record, dict) else None
            if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model_id):
                raise UpstreamAdapterError("provider model catalog is invalid")
            model_ids.append(model_id)
        if len(model_ids) != len(set(model_ids)):
            raise UpstreamAdapterError("provider model catalog is invalid")
        return sorted(model_ids)


class OpenAICompletionsAdapter:
    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        if not isinstance(timeout_seconds, (int, float)) or not 1 <= float(timeout_seconds) <= 600:
            raise ValueError("upstream timeout is invalid")
        self._timeout_seconds = float(timeout_seconds)

    def complete(
        self,
        route: GatewayRoute,
        secret: bytes,
        request: dict[str, object],
        *, cancellation: RequestCancellation | None = None,
    ) -> dict[str, object]:
        response = self._request(route, secret, request, cancellation)
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_UPSTREAM_RESPONSE_BYTES:
                    response.close()
                    raise UpstreamAdapterError("upstream response is too large")
            except ValueError:
                response.close()
                raise UpstreamAdapterError("upstream response metadata is invalid") from None
        chunks: list[bytes] = []
        size = 0
        try:
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > MAX_UPSTREAM_RESPONSE_BYTES:
                    raise UpstreamAdapterError("upstream response is too large")
                chunks.append(chunk)
        finally:
            response.close()
        try:
            payload = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamAdapterError("upstream response is invalid") from exc
        if not isinstance(payload, dict):
            raise UpstreamAdapterError("upstream response is invalid")
        return payload

    def stream(
        self,
        route: GatewayRoute,
        secret: bytes,
        request: dict[str, object],
        *, cancellation: RequestCancellation | None = None,
    ):
        response = self._request(route, secret, request, cancellation)
        media_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media_type != "text/event-stream":
            response.close()
            raise UpstreamAdapterError("upstream stream content type is invalid")
        total = 0
        try:
            for chunk in response.iter_bytes(64 * 1024):
                if not isinstance(chunk, bytes):
                    raise UpstreamAdapterError("upstream stream is invalid")
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_UPSTREAM_STREAM_BYTES:
                    raise UpstreamAdapterError("upstream stream is too large")
                yield chunk
        finally:
            response.close()

    def _request(
        self,
        route: GatewayRoute,
        secret: bytes,
        request: dict[str, object],
        cancellation: RequestCancellation | None,
    ):
        try:
            credential = secret.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UpstreamAdapterError("upstream credential is invalid") from exc
        try:
            completion_path = "/chat/completions" if route.upstream_provider == "deepseek" else "/v1/chat/completions"
            response = open_upstream(
                f"{route.endpoint_origin}{completion_path}",
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
                payload=request,
                timeout_seconds=self._timeout_seconds,
                cancellation=cancellation,
            )
        except UpstreamTransportError as exc:
            raise UpstreamAdapterError("upstream request failed") from exc
        if response.status < 200 or response.status >= 300:
            response.close()
            raise UpstreamAdapterError("upstream request failed")
        return response
