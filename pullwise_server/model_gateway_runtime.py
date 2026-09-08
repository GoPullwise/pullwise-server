from __future__ import annotations

import json
import re
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from .model_gateway_token_codec import GatewayTokenVerifier
from .model_gateway_limits import GatewayLimitExceeded, GatewayLimiter
from .model_gateway_endpoints import OFFICIAL_PROVIDER_ORIGINS
from .model_gateway_usage import CompletionStreamUsage, reported_output_tokens
from .model_gateway_streaming import ActiveResponses, ManagedResponse, RequestCancellation


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")
SECRET_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
CHAT_COMPLETION_FIELDS = frozenset(
    {
        "model",
        "messages",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "max_completion_tokens",
        "max_tokens",
        "n",
        "parallel_tool_calls",
        "presence_penalty",
        "response_format",
        "seed",
        "service_tier",
        "stop",
        "store",
        "stream",
        "stream_options",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
        "user",
        "reasoning_effort",
        "thinking",
        "metadata",
    }
)
MAX_REQUEST_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024 * 1024


class GatewayRequestError(ValueError):
    def __init__(self, code: str, *, http_status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class SecretStore(Protocol):
    def read_version(self, secret_ref: str, version: str) -> bytes: ...


class GatewayAdapter(Protocol):
    def complete(
        self,
        route: "GatewayRoute",
        secret: bytes,
        request: dict[str, object],
        *, cancellation: RequestCancellation | None = None,
    ) -> dict[str, object]: ...

    def stream(
        self,
        route: "GatewayRoute",
        secret: bytes,
        request: dict[str, object],
        *, cancellation: RequestCancellation | None = None,
    ) -> Iterator[bytes]: ...


class GatewayRouteResolver(Protocol):
    def resolve(
        self,
        profile_set_id: str,
        profile_revision: int,
        model_alias: str,
    ) -> "GatewayRoute | None": ...


@dataclass(frozen=True)
class GatewayStreamingResponse:
    chunks: Iterable[bytes]
    content_type: str = "text/event-stream; charset=utf-8"


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise GatewayRequestError(f"{label.upper()}_INVALID")
    return value


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GatewayRequestError(f"{label.upper()}_INVALID")
    return value


def _origin(value: object) -> str:
    if not isinstance(value, str):
        raise GatewayRequestError("GATEWAY_ROUTE_ORIGIN_INVALID")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise GatewayRequestError("GATEWAY_ROUTE_ORIGIN_INVALID")
    try:
        port = parsed.port
    except ValueError as exc:
        raise GatewayRequestError("GATEWAY_ROUTE_ORIGIN_INVALID") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return urlunsplit(("https", f"{host}:{port}" if port is not None else host, "", "", ""))


@dataclass(frozen=True)
class GatewayRoute:
    route_id: str
    profile_set_id: str
    profile_revision: int
    manifest_digest: str
    model_alias: str
    provider_connection_id: str
    upstream_provider: str
    adapter: str
    endpoint_origin: str
    upstream_model: str
    secret_ref: str
    secret_version: str

    def __post_init__(self) -> None:
        for field in (
            "route_id",
            "profile_set_id",
            "model_alias",
            "provider_connection_id",
            "upstream_provider",
            "upstream_model",
            "secret_version",
        ):
            _safe_id(getattr(self, field), f"gateway_route_{field}")
        _positive_integer(self.profile_revision, "gateway_route_revision")
        if not SHA256_DIGEST.fullmatch(self.manifest_digest):
            raise GatewayRequestError("GATEWAY_ROUTE_MANIFEST_DIGEST_INVALID")
        if self.adapter != "openai-completions":
            raise GatewayRequestError("GATEWAY_ROUTE_ADAPTER_UNSUPPORTED")
        if not SECRET_REF.fullmatch(self.secret_ref):
            raise GatewayRequestError("GATEWAY_ROUTE_SECRET_REF_INVALID")
        normalized_origin = _origin(self.endpoint_origin)
        if (
            normalized_origin != self.endpoint_origin
            or self.upstream_provider not in OFFICIAL_PROVIDER_ORIGINS
            or normalized_origin not in OFFICIAL_PROVIDER_ORIGINS[self.upstream_provider]
        ):
            raise GatewayRequestError("GATEWAY_ROUTE_ORIGIN_NOT_CANONICAL")


class StaticGatewayRouteResolver:
    def __init__(self, routes: list[GatewayRoute]) -> None:
        if not routes:
            raise GatewayRequestError("GATEWAY_ROUTES_EMPTY")
        self._routes: dict[tuple[str, int, str], GatewayRoute] = {}
        for route in routes:
            key = (route.profile_set_id, route.profile_revision, route.model_alias)
            if key in self._routes:
                raise GatewayRequestError("GATEWAY_ROUTE_AMBIGUOUS")
            self._routes[key] = route

    def resolve(
        self,
        profile_set_id: str,
        profile_revision: int,
        model_alias: str,
    ) -> GatewayRoute | None:
        return self._routes.get((profile_set_id, profile_revision, model_alias))


class ModelGatewayRuntime:
    def __init__(
        self,
        *,
        token_verifier: GatewayTokenVerifier,
        route_resolver: GatewayRouteResolver,
        secret_store: SecretStore,
        adapters: Mapping[str, GatewayAdapter],
        audit_sink: Callable[[dict[str, object]], None],
        request_id_factory: Callable[[], str],
        clock: Callable[[], float],
        limiter: GatewayLimiter,
    ) -> None:
        self._token_verifier = token_verifier
        self._route_resolver = route_resolver
        self._secret_store = secret_store
        self._adapters = dict(adapters)
        self._audit_sink = audit_sink
        self._request_id_factory = request_id_factory
        self._clock = clock
        self._limiter = limiter
        self._active_responses = ActiveResponses()

    def chat_completions(
        self,
        *,
        worker_id: str,
        profile_set_id: str,
        profile_revision: int,
        bearer_token: str,
        payload: object,
        is_disconnected: Callable[[], bool] | None = None,
    ) -> dict[str, object] | GatewayStreamingResponse:
        worker = _safe_id(worker_id, "gateway_request_worker")
        profile_set = _safe_id(profile_set_id, "gateway_request_profile_set")
        revision = _positive_integer(profile_revision, "gateway_request_profile_revision")
        if not isinstance(payload, dict):
            raise GatewayRequestError("GATEWAY_REQUEST_BODY_INVALID")
        unknown = set(payload) - CHAT_COMPLETION_FIELDS
        if unknown:
            raise GatewayRequestError("GATEWAY_REQUEST_FIELD_UNSUPPORTED")
        model_alias = _safe_id(payload.get("model"), "gateway_request_model")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 512:
            raise GatewayRequestError("GATEWAY_REQUEST_MESSAGES_INVALID")
        streaming = payload.get("stream", False)
        if not isinstance(streaming, bool):
            raise GatewayRequestError("GATEWAY_STREAM_INVALID")
        output_limits = [payload[key] for key in ("max_tokens", "max_completion_tokens") if key in payload]
        if len(output_limits) > 1:
            raise GatewayRequestError("GATEWAY_OUTPUT_TOKEN_LIMIT_AMBIGUOUS")
        output_tokens = output_limits[0] if output_limits else 16_384
        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or not 1 <= output_tokens <= 1_000_000:
            raise GatewayRequestError("GATEWAY_OUTPUT_TOKEN_LIMIT_INVALID")
        try:
            request_bytes = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise GatewayRequestError("GATEWAY_REQUEST_BODY_INVALID") from exc
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise GatewayRequestError("GATEWAY_REQUEST_TOO_LARGE")
        route = self._route_resolver.resolve(profile_set, revision, model_alias)
        if route is None:
            raise GatewayRequestError("GATEWAY_ROUTE_NOT_FOUND")
        if (
            route.profile_set_id != profile_set
            or route.profile_revision != revision
            or route.model_alias != model_alias
            or route.adapter not in self._adapters
        ):
            raise GatewayRequestError("GATEWAY_ROUTE_INVALID")
        self._token_verifier.verify(
            bearer_token,
            worker_id=worker,
            profile_set_id=profile_set,
            profile_revision=revision,
            manifest_digest=route.manifest_digest,
            route_id=route.route_id,
        )
        request_id = _safe_id(self._request_id_factory(), "gateway_request_id")
        started_at = self._clock()
        managed = self._managed_response(request_id=request_id, worker_id=worker, route=route,
            payload=payload, started_at=started_at, output_tokens=output_tokens,
            streaming=streaming, is_disconnected=is_disconnected)
        if streaming:
            return GatewayStreamingResponse(chunks=managed)
        responses = list(managed)
        if not responses:
            raise GatewayRequestError("GATEWAY_REQUEST_CANCELLED", http_status=499)
        return responses[0]

    def _managed_response(
        self,
        *,
        request_id: str,
        worker_id: str,
        route: GatewayRoute,
        payload: dict[str, object],
        started_at: float,
        output_tokens: int,
        streaming: bool,
        is_disconnected: Callable[[], bool] | None,
    ) -> ManagedResponse:
        self._active_responses.prune_disconnected(worker_id, route.route_id)
        reservation = ExitStack()
        try:
            settle = reservation.enter_context(self._limiter.acquire(
                worker_id=worker_id, route_id=route.route_id, output_tokens=output_tokens))
        except GatewayLimitExceeded as exc:
            self._audit_sink(self._audit_event(request_id, worker_id, route, started_at, "limited"))
            raise GatewayRequestError(exc.code, http_status=429) from exc
        actual_output: int | None = None

        def produce(cancellation: RequestCancellation):
            nonlocal actual_output
            secret = self._secret_store.read_version(route.secret_ref, route.secret_version)
            upstream_request = {**payload, "model": route.upstream_model}
            if not streaming:
                response = self._adapters[route.adapter].complete(route, secret, upstream_request, cancellation=cancellation)
                if not isinstance(response, dict):
                    raise GatewayRequestError("GATEWAY_UPSTREAM_RESPONSE_INVALID")
                actual_output = reported_output_tokens(response)
                yield response
                return
            total = 0
            usage = CompletionStreamUsage()
            for chunk in self._adapters[route.adapter].stream(route, secret, upstream_request, cancellation=cancellation):
                if cancellation.is_set():
                    return
                if not isinstance(chunk, bytes):
                    raise GatewayRequestError("GATEWAY_UPSTREAM_STREAM_INVALID")
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_STREAM_BYTES:
                    raise GatewayRequestError("GATEWAY_UPSTREAM_STREAM_TOO_LARGE")
                usage.feed(chunk)
                yield chunk
            actual_output = usage.output_tokens

        def finish(outcome: str) -> None:
            try:
                if outcome == "succeeded" and actual_output is not None:
                    settle(actual_output)
            finally:
                reservation.close()
                self._active_responses.remove(worker_id, route.route_id, response)
                self._audit_sink(self._audit_event(request_id, worker_id, route, started_at, outcome))

        response = ManagedResponse(producer=produce, finish=finish, is_disconnected=is_disconnected)
        self._active_responses.add(worker_id, route.route_id, response)
        return response

    def _audit_event(
        self,
        request_id: str,
        worker_id: str,
        route: GatewayRoute,
        started_at: float,
        outcome: str,
    ) -> dict[str, object]:
        completed_at = self._clock()
        return {
            "schema_id": "pullwise-model-gateway-audit/v1",
            "request_id": request_id,
            "worker_id": worker_id,
            "profile_set_id": route.profile_set_id,
            "profile_revision": route.profile_revision,
            "manifest_digest": route.manifest_digest,
            "route_id": route.route_id,
            "model_alias": route.model_alias,
            "upstream_model": route.upstream_model,
            "provider_connection_id": route.provider_connection_id,
            "outcome": outcome,
            "duration_ms": max(0, int((completed_at - started_at) * 1000)),
        }
