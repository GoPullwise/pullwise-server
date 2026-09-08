from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from pullwise_server.model_gateway_limits import GatewayLimitExceeded, GatewayLimiter, GatewayLimitPolicy
from pullwise_server.model_gateway_runtime import GatewayRequestError, GatewayRoute, ModelGatewayRuntime, StaticGatewayRouteResolver
from pullwise_server.model_gateway_usage import CompletionStreamUsage


class UsageAdapter:
    def __init__(self, usage: object) -> None:
        self.usage = usage

    def complete(self, *_args, **_kwargs):
        return {"choices": [], "usage": self.usage}

    def stream(self, *_args, **_kwargs):
        wire = b'data: {"choices":[]}\r\n\r\n' + b'data: ' + json.dumps({"usage": self.usage}).encode() + b'\r\n\r\ndata: [DONE]\r\n\r\n'
        for index in range(0, len(wire), 7):
            yield wire[index:index + 7]


def gateway(usage: object):
    route = GatewayRoute(
        route_id="route", profile_set_id="profile", profile_revision=1, manifest_digest="a" * 64,
        model_alias="model", provider_connection_id="provider", upstream_provider="openai",
        adapter="openai-completions", endpoint_origin="https://api.openai.com", upstream_model="model",
        secret_ref="provider/key", secret_version="v1",
    )
    return ModelGatewayRuntime(
        token_verifier=SimpleNamespace(verify=lambda *_args, **_kwargs: None),
        route_resolver=StaticGatewayRouteResolver([route]),
        secret_store=SimpleNamespace(read_version=lambda *_args: b"test-key"),
        adapters={"openai-completions": UsageAdapter(usage)}, audit_sink=lambda _event: None,
        request_id_factory=lambda: "request", clock=lambda: 1,
        limiter=GatewayLimiter(policy=GatewayLimitPolicy(60, 60, 1, 1, 150, 150), clock=lambda: 1),
    )


def request(runtime, streaming: bool):
    response = runtime.chat_completions(worker_id="worker", profile_set_id="profile", profile_revision=1,
        bearer_token="test-token", payload={"model": "model", "messages": [{"role": "user", "content": "review"}],
        "max_tokens": 100, "stream": streaming})
    if streaming:
        return b"".join(response.chunks)
    return response


class ModelGatewayUsageTest(unittest.TestCase):
    def test_reservation_settlement_is_once_only_and_cannot_refund_a_new_window(self):
        now = [1]
        limiter = GatewayLimiter(policy=GatewayLimitPolicy(60, 60, 2, 3, 150, 150), clock=lambda: now[0])
        with limiter.acquire(worker_id="worker", route_id="route", output_tokens=100) as settle:
            settle(10)
            settle(0)
            with self.assertRaisesRegex(GatewayLimitExceeded, "GATEWAY_BUDGET_EXCEEDED"):
                with limiter.acquire(worker_id="worker", route_id="route", output_tokens=145):
                    pass
        with limiter.acquire(worker_id="worker", route_id="route", output_tokens=100) as old:
            now[0] = 61
            with limiter.acquire(worker_id="next", route_id="route", output_tokens=100):
                old(0)
                with self.assertRaisesRegex(GatewayLimitExceeded, "GATEWAY_BUDGET_EXCEEDED"):
                    with limiter.acquire(worker_id="third", route_id="route", output_tokens=100):
                        pass

    def test_oversized_frames_are_discarded_and_later_usage_remains_readable(self):
        usage = CompletionStreamUsage()
        usage.feed(b"data: " + b"x" * (usage.MAX_FRAME_BYTES + 1))
        self.assertLessEqual(len(usage._buffer), usage.MAX_FRAME_BYTES)
        usage.feed(b'\n\ndata: {"usage":{"completion_tokens":7}}\n\n')
        self.assertEqual(usage.output_tokens, 7)

    def test_completed_requests_charge_actual_output_instead_of_every_declared_ceiling(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                runtime = gateway({"completion_tokens": 10})
                for _ in range(3):
                    result = request(runtime, streaming)
                    if streaming:
                        self.assertIn(b"[DONE]", result)

    def test_missing_or_invalid_usage_keeps_the_full_reservation(self):
        for usage in (None, {}, {"completion_tokens": -1}, {"completion_tokens": True}):
            for streaming in (False, True):
                with self.subTest(usage=usage, streaming=streaming):
                    runtime = gateway(usage)
                    request(runtime, streaming)
                    with self.assertRaisesRegex(GatewayRequestError, "GATEWAY_BUDGET_EXCEEDED"):
                        request(runtime, streaming)
