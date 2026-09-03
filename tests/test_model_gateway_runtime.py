from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server import db
from pullwise_server.model_gateway_runtime import (
    GatewayRoute,
    GatewayStreamingResponse,
    ModelGatewayRuntime,
    StaticGatewayRouteResolver,
)
from pullwise_server.model_gateway_limits import GatewayLimitExceeded, GatewayLimiter, GatewayLimitPolicy
from pullwise_server.model_gateway_runtime import GatewayRequestError
from pullwise_server.model_gateway_tokens import (
    GatewayGrantStatus,
    GatewayTokenAuthority,
    GatewayTokenVerifier,
)
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections


class FakeSecretStore:
    def __init__(self, expected_ref: str, expected_version: str, secret: bytes) -> None:
        self.expected_ref = expected_ref
        self.expected_version = expected_version
        self.secret = secret
        self.reads = 0

    def read_version(self, secret_ref: str, version: str) -> bytes:
        if secret_ref != self.expected_ref or version != self.expected_version:
            raise AssertionError("unexpected secret lookup")
        self.reads += 1
        return self.secret


class UnavailableSecretStore:
    def read_version(self, _secret_ref: str, _version: str) -> bytes:
        raise RuntimeError("secret store unavailable")


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[GatewayRoute, bytes, dict[str, object]]] = []

    def complete(
        self,
        route: GatewayRoute,
        secret: bytes,
        request: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((route, secret, request))
        return {
            "id": "chatcmpl_fake",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "safe response"}}],
        }

    def stream(
        self,
        route: GatewayRoute,
        secret: bytes,
        request: dict[str, object],
    ):
        self.calls.append((route, secret, request))
        yield b'data: {"id":"chunk-1"}\n\n'
        yield b"data: [DONE]\n\n"


class ModelGatewayRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)

    def test_allowlisted_route_injects_secret_only_at_adapter_boundary(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        authority = GatewayTokenAuthority(
            connect_factory=db.connect,
            private_key=private_key,
            key_id="gateway-signing-2026-09",
            issuer="https://api.pull-wise.com",
            ttl_seconds=300,
            clock=lambda: 1_788_259_200,
            jti_factory=lambda: "gtj_gateway_runtime",
        )
        grant = authority.issue(
            worker_id="worker_a",
            profile_set_id="reviewer-production",
            profile_revision=12,
            manifest_digest="c" * 64,
            route_ids=["gpt-primary"],
            generation=3,
        )
        verifier = GatewayTokenVerifier(
            public_keys={"gateway-signing-2026-09": private_key.public_key()},
            issuer="https://api.pull-wise.com",
            clock=lambda: 1_788_259_201,
            grant_status_resolver=lambda _jti, _hash: GatewayGrantStatus(
                active=True,
                worker_enabled=True,
                generation=3,
                desired_profile_revision=12,
            ),
        )
        secret = b"upstream-secret-SENTINEL-runtime"
        store = FakeSecretStore("provider/openai-production/secret_1", "version-1", secret)
        adapter = FakeAdapter()
        audit_events: list[dict[str, object]] = []
        route = GatewayRoute(
            route_id="gpt-primary",
            profile_set_id="reviewer-production",
            profile_revision=12,
            manifest_digest="c" * 64,
            model_alias="gpt-reviewer",
            provider_connection_id="openai-production",
            upstream_provider="openai",
            adapter="openai-completions",
            endpoint_origin="https://api.openai.com",
            upstream_model="gpt-5.5",
            secret_ref="provider/openai-production/secret_1",
            secret_version="version-1",
        )
        gateway = ModelGatewayRuntime(
            token_verifier=verifier,
            route_resolver=StaticGatewayRouteResolver([route]),
            secret_store=store,
            adapters={"openai-completions": adapter},
            audit_sink=audit_events.append,
            request_id_factory=lambda: "mgr_fixed",
            clock=lambda: 1_788_259_201.25,
            limiter=GatewayLimiter(
                policy=GatewayLimitPolicy(
                    worker_requests_per_minute=10,
                    route_requests_per_minute=10,
                    worker_concurrency=1,
                    route_concurrency=2,
                    worker_output_tokens_per_minute=100_000,
                    route_output_tokens_per_minute=100_000,
                ),
                clock=lambda: 1_788_259_201.25,
            ),
        )

        response = gateway.chat_completions(
            worker_id="worker_a",
            profile_set_id="reviewer-production",
            profile_revision=12,
            bearer_token=grant.token,
            payload={
                "model": "gpt-reviewer",
                "messages": [{"role": "user", "content": "sensitive prompt text"}],
                "stream": False,
            },
        )

        self.assertEqual(response["id"], "chatcmpl_fake")
        self.assertEqual(store.reads, 1)
        self.assertEqual(len(adapter.calls), 1)
        called_route, called_secret, called_payload = adapter.calls[0]
        self.assertEqual(called_route.endpoint_origin, "https://api.openai.com")
        self.assertEqual(called_secret, secret)
        self.assertEqual(called_payload["model"], "gpt-5.5")
        self.assertNotIn("endpoint_origin", called_payload)
        self.assertNotIn("headers", called_payload)
        self.assertEqual(len(audit_events), 1)
        serialized_audit = json.dumps(audit_events, sort_keys=True)
        self.assertNotIn(secret.decode("ascii"), serialized_audit)
        self.assertNotIn("sensitive prompt text", serialized_audit)
        self.assertNotIn("safe response", serialized_audit)

        streaming = gateway.chat_completions(
            worker_id="worker_a",
            profile_set_id="reviewer-production",
            profile_revision=12,
            bearer_token=grant.token,
            payload={
                "model": "gpt-reviewer",
                "messages": [{"role": "user", "content": "streaming prompt"}],
                "stream": True,
            },
        )
        self.assertIsInstance(streaming, GatewayStreamingResponse)
        self.assertEqual(
            list(streaming.chunks),
            [b'data: {"id":"chunk-1"}\n\n', b"data: [DONE]\n\n"],
        )
        self.assertEqual(audit_events[-1]["outcome"], "succeeded")
        self.assertNotIn("chunk-1", json.dumps(audit_events[-1]))

        limited_gateway = ModelGatewayRuntime(
            token_verifier=verifier,
            route_resolver=StaticGatewayRouteResolver([route]),
            secret_store=store,
            adapters={"openai-completions": adapter},
            audit_sink=audit_events.append,
            request_id_factory=lambda: "mgr_limited",
            clock=lambda: 1_788_259_201.25,
            limiter=GatewayLimiter(
                policy=GatewayLimitPolicy(
                    worker_requests_per_minute=1,
                    route_requests_per_minute=1,
                    worker_concurrency=1,
                    route_concurrency=1,
                    worker_output_tokens_per_minute=100_000,
                    route_output_tokens_per_minute=100_000,
                ),
                clock=lambda: 1_788_259_201.25,
            ),
        )
        limited_gateway.chat_completions(
            worker_id="worker_a",
            profile_set_id="reviewer-production",
            profile_revision=12,
            bearer_token=grant.token,
            payload={"model": "gpt-reviewer", "messages": [{"role": "user", "content": "first"}]},
        )
        reads_before_rejection = store.reads
        with self.assertRaisesRegex(GatewayRequestError, "GATEWAY_RATE_LIMITED"):
            limited_gateway.chat_completions(
                worker_id="worker_a",
                profile_set_id="reviewer-production",
                profile_revision=12,
                bearer_token=grant.token,
                payload={"model": "gpt-reviewer", "messages": [{"role": "user", "content": "second"}]},
            )
        self.assertEqual(store.reads, reads_before_rejection)

        budget_gateway = ModelGatewayRuntime(
            token_verifier=verifier,
            route_resolver=StaticGatewayRouteResolver([route]),
            secret_store=store,
            adapters={"openai-completions": adapter},
            audit_sink=audit_events.append,
            request_id_factory=lambda: "mgr_budget",
            clock=lambda: 1_788_259_201.25,
            limiter=GatewayLimiter(
                policy=GatewayLimitPolicy(
                    worker_requests_per_minute=10,
                    route_requests_per_minute=10,
                    worker_concurrency=1,
                    route_concurrency=1,
                    worker_output_tokens_per_minute=10,
                    route_output_tokens_per_minute=10,
                ),
                clock=lambda: 1_788_259_201.25,
            ),
        )
        reads_before_budget = store.reads
        with self.assertRaisesRegex(GatewayRequestError, "GATEWAY_BUDGET_EXCEEDED"):
            budget_gateway.chat_completions(
                worker_id="worker_a",
                profile_set_id="reviewer-production",
                profile_revision=12,
                bearer_token=grant.token,
                payload={
                    "model": "gpt-reviewer",
                    "messages": [{"role": "user", "content": "budget"}],
                    "max_tokens": 11,
                },
            )
        self.assertEqual(store.reads, reads_before_budget)

        unavailable_events: list[dict[str, object]] = []
        unavailable_adapter = FakeAdapter()
        unavailable_gateway = ModelGatewayRuntime(
            token_verifier=verifier,
            route_resolver=StaticGatewayRouteResolver([route]),
            secret_store=UnavailableSecretStore(),
            adapters={"openai-completions": unavailable_adapter},
            audit_sink=unavailable_events.append,
            request_id_factory=lambda: "mgr_secret_unavailable",
            clock=lambda: 1_788_259_201.25,
            limiter=GatewayLimiter(
                policy=GatewayLimitPolicy(
                    worker_requests_per_minute=10,
                    route_requests_per_minute=10,
                    worker_concurrency=1,
                    route_concurrency=1,
                    worker_output_tokens_per_minute=100_000,
                    route_output_tokens_per_minute=100_000,
                ),
                clock=lambda: 1_788_259_201.25,
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "secret store unavailable"):
            unavailable_gateway.chat_completions(
                worker_id="worker_a",
                profile_set_id="reviewer-production",
                profile_revision=12,
                bearer_token=grant.token,
                payload={"model": "gpt-reviewer", "messages": [{"role": "user", "content": "secret"}]},
            )
        self.assertEqual(unavailable_adapter.calls, [])
        self.assertEqual(unavailable_events[-1]["outcome"], "failed")

    def test_limiter_rejects_overlapping_worker_and_route_requests(self) -> None:
        limiter = GatewayLimiter(
            policy=GatewayLimitPolicy(
                worker_requests_per_minute=10,
                route_requests_per_minute=10,
                worker_concurrency=1,
                route_concurrency=1,
                worker_output_tokens_per_minute=100,
                route_output_tokens_per_minute=100,
            ),
            clock=lambda: 1_788_259_201.25,
        )
        with limiter.acquire(worker_id="worker_a", route_id="gpt-primary", output_tokens=1):
            with self.assertRaisesRegex(GatewayLimitExceeded, "GATEWAY_CONCURRENCY_LIMITED"):
                with limiter.acquire(worker_id="worker_a", route_id="gpt-primary", output_tokens=1):
                    pass


if __name__ == "__main__":
    unittest.main()
