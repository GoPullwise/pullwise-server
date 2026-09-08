from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable, Mapping, Protocol
from urllib.parse import unquote, urlsplit

from .model_gateway_runtime import GatewayRequestError, GatewayStreamingResponse
from .model_gateway_secret_broker import SecretBrokerError
from .model_gateway_token_codec import GatewayTokenError
from .model_gateway_client_connection import ClientConnection


COMPLETION_PATH = re.compile(
    r"^/v1/workers/([^/]+)/profiles/([^/]+)/revisions/([1-9][0-9]*)/chat/completions$"
)
MAX_BROKER_BODY_BYTES = 32 * 1024
MAX_COMPLETION_BODY_BYTES = 1024 * 1024


class Runtime(Protocol):
    def chat_completions(self, **kwargs: object) -> dict[str, object] | GatewayStreamingResponse: ...


class SecretBroker(Protocol):
    def put_candidate(self, **kwargs: object) -> dict[str, object]: ...


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: dict[str, object]
    headers: Mapping[str, str]


@dataclass(frozen=True)
class HttpStreamingResult:
    status: int
    chunks: Iterable[bytes]
    content_type: str
    headers: Mapping[str, str]


def _header(headers: Mapping[str, str], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return ""


def _bearer(headers: Mapping[str, str]) -> str:
    value = _header(headers, "Authorization")
    parts = value.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise GatewayTokenError("GATEWAY_TOKEN_MISSING")
    return parts[1]


def _json_body(body: bytes, *, limit: int) -> dict[str, object]:
    if not isinstance(body, bytes) or not body or len(body) > limit:
        raise GatewayRequestError("GATEWAY_REQUEST_BODY_INVALID")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise GatewayRequestError("GATEWAY_REQUEST_BODY_INVALID")
            result[key] = value
        return result

    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, GatewayRequestError):
            raise
        raise GatewayRequestError("GATEWAY_REQUEST_BODY_INVALID") from exc
    if not isinstance(payload, dict):
        raise GatewayRequestError("GATEWAY_REQUEST_BODY_INVALID")
    return payload


def _error(status: int, code: str) -> HttpResult:
    return HttpResult(
        status=status,
        payload={"error": {"code": code, "message": "Model Gateway request rejected."}},
        headers={"Cache-Control": "no-store"},
    )


class ModelGatewayHttpApplication:
    def __init__(self, *, runtime: Runtime, secret_broker: SecretBroker) -> None:
        self._runtime = runtime
        self._secret_broker = secret_broker

    def dispatch(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        is_disconnected: Callable[[], bool] | None = None,
    ) -> HttpResult | HttpStreamingResult:
        parsed_path = urlsplit(path)
        if parsed_path.query or parsed_path.fragment:
            return _error(HTTPStatus.NOT_FOUND, "GATEWAY_ROUTE_NOT_FOUND")
        if method == "GET" and parsed_path.path == "/health":
            return HttpResult(
                status=HTTPStatus.OK,
                payload={"ok": True, "service": "pullwise-model-gateway"},
                headers={"Cache-Control": "no-store"},
            )
        broker_paths = {
            "/internal/provider-secrets": ("put_candidate", HTTPStatus.CREATED),
            "/internal/provider-secrets/validate": ("validate_candidate", HTTPStatus.OK),
            "/internal/provider-secrets/canary": ("canary_candidate", HTTPStatus.OK),
            "/internal/provider-secrets/retire": ("retire_version", HTTPStatus.OK),
        }
        if method == "POST" and parsed_path.path in broker_paths:
            try:
                payload = _json_body(body, limit=MAX_BROKER_BODY_BYTES)
                method_name, success_status = broker_paths[parsed_path.path]
                response = getattr(self._secret_broker, method_name)(
                    authorization=_header(headers, "Authorization"),
                    payload=payload,
                )
                return HttpResult(
                    status=success_status,
                    payload=response,
                    headers={"Cache-Control": "no-store"},
                )
            except SecretBrokerError:
                return _error(HTTPStatus.UNAUTHORIZED, "GATEWAY_BROKER_REQUEST_REJECTED")
            except GatewayRequestError as exc:
                return _error(HTTPStatus.BAD_REQUEST, exc.code)
        match = COMPLETION_PATH.fullmatch(parsed_path.path) if method == "POST" else None
        if not match:
            return _error(HTTPStatus.NOT_FOUND, "GATEWAY_ROUTE_NOT_FOUND")
        worker_id, profile_set_id, revision_text = (unquote(value) for value in match.groups())
        try:
            payload = _json_body(body, limit=MAX_COMPLETION_BODY_BYTES)
            response = self._runtime.chat_completions(
                worker_id=worker_id,
                profile_set_id=profile_set_id,
                profile_revision=int(revision_text),
                bearer_token=_bearer(headers),
                payload=payload,
                **({"is_disconnected": is_disconnected} if is_disconnected is not None else {}),
            )
            if isinstance(response, GatewayStreamingResponse):
                return HttpStreamingResult(
                    status=HTTPStatus.OK,
                    chunks=response.chunks,
                    content_type=response.content_type,
                    headers={"Cache-Control": "no-store", "Connection": "close"},
                )
            return HttpResult(
                status=HTTPStatus.OK,
                payload=response,
                headers={"Cache-Control": "no-store"},
            )
        except GatewayTokenError as exc:
            return _error(HTTPStatus.UNAUTHORIZED, exc.code)
        except GatewayRequestError as exc:
            return _error(exc.http_status, exc.code)
        except Exception:
            return _error(HTTPStatus.BAD_GATEWAY, "GATEWAY_UPSTREAM_FAILED")


def make_handler(application: ModelGatewayHttpApplication) -> type[BaseHTTPRequestHandler]:
    class ModelGatewayHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def _handle(self, method: str) -> None:
            client = ClientConnection(self.connection)
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_COMPLETION_BODY_BYTES:
                result = _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "GATEWAY_REQUEST_TOO_LARGE")
            else:
                result = application.dispatch(
                    method=method,
                    path=self.path,
                    headers={key: value for key, value in self.headers.items()},
                    body=self.rfile.read(length) if length else b"",
                    is_disconnected=client.disconnected,
                )
            if isinstance(result, HttpStreamingResult):
                iterator = iter(result.chunks)
                try:
                    with client.lock:
                        self.send_response(result.status)
                        self.send_header("Content-Type", result.content_type)
                        for key, value in result.headers.items():
                            self.send_header(key, value)
                        self.end_headers()
                    for chunk in iterator:
                        with client.lock:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    code = error.code if isinstance(error, GatewayRequestError) else "GATEWAY_UPSTREAM_FAILED"
                    try:
                        with client.lock:
                            payload = json.dumps({"error": {"code": code, "message": "Model Gateway stream failed."}})
                            self.wfile.write(f"data: {payload}\n\ndata: [DONE]\n\n".encode("utf-8"))
                            self.wfile.flush()
                    except OSError:
                        pass
                finally:
                    close = getattr(iterator, "close", None)
                    if callable(close):
                        close()
                    self.close_connection = True
                return
            encoded = json.dumps(result.payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            self.send_response(result.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            for key, value in result.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return ModelGatewayHandler


def serve(application: ModelGatewayHttpApplication, *, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(application))
    server.serve_forever()
