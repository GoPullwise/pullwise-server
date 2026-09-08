from __future__ import annotations

import itertools
import json
import threading
import time
import unittest
from types import SimpleNamespace

from pullwise_server.model_gateway_http import ModelGatewayHttpApplication
from pullwise_server.model_gateway_limits import GatewayLimiter, GatewayLimitPolicy
from pullwise_server.model_gateway_runtime import GatewayRoute, ModelGatewayRuntime, StaticGatewayRouteResolver


class Adapter:
    def __init__(self):
        self.started = threading.Event()
        self.stopped = threading.Event()

    def stream(self, _route, _secret, request, *, cancellation=None):
        if request["messages"][0]["content"] == "stall":
            self.started.set()
            if cancellation is not None:
                cancellation.add_callback(self.stopped.set)
                cancellation.wait(5)
            else:
                self.stopped.wait(5)
            yield b"late data must not reach the cancelled client"
        else:
            yield b"data: [DONE]\n\n"

    def complete(self, *_args, **_kwargs):
        return {"choices": []}


class GatewayStreamLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter()
        self.audit = []
        identifiers = itertools.count()
        self.route = GatewayRoute(route_id="route", profile_set_id="profile", profile_revision=1,
            manifest_digest="a"*64, model_alias="model", provider_connection_id="connection",
            upstream_provider="openai", adapter="openai-completions", endpoint_origin="https://api.openai.com",
            upstream_model="gpt-5.5", secret_ref="provider/key", secret_version="v1")
        self.runtime = ModelGatewayRuntime(token_verifier=SimpleNamespace(verify=lambda *_args, **_kwargs: {}),
            route_resolver=StaticGatewayRouteResolver([self.route]),
            secret_store=SimpleNamespace(read_version=lambda *_args: b"local-fixture"),
            adapters={"openai-completions": self.adapter}, audit_sink=self.audit.append,
            request_id_factory=lambda: f"request_{next(identifiers)}", clock=time.time,
            limiter=GatewayLimiter(policy=GatewayLimitPolicy(100,100,1,2,1000000,1000000), clock=time.time))

    def payload(self, text="normal"):
        return {"model":"model", "messages":[{"role":"user", "content":text}], "stream":True}

    def stream(self, text="normal", **kwargs):
        return self.runtime.chat_completions(worker_id="worker", profile_set_id="profile", profile_revision=1,
            bearer_token="fixture-token", payload=self.payload(text), **kwargs)

    def test_http_admission_rejects_overlap_before_returning_200(self):
        application = ModelGatewayHttpApplication(runtime=self.runtime, secret_broker=object())
        args = dict(method="POST", path="/v1/workers/worker/profiles/profile/revisions/1/chat/completions",
            headers={"Authorization":"Bearer fixture-token"}, body=json.dumps(self.payload()).encode())
        first = application.dispatch(**args)
        try:
            second = application.dispatch(**args)
            self.assertEqual(second.status, 429)
            self.assertEqual(second.payload["error"]["code"], "GATEWAY_CONCURRENCY_LIMITED")
        finally:
            first.chunks.close()

    def test_closing_a_dormant_stream_releases_its_reservation(self):
        first = self.stream()
        first.chunks.close()
        self.assertEqual(list(self.stream().chunks), [b"data: [DONE]\n\n"])
        self.assertEqual([event["outcome"] for event in self.audit], ["cancelled", "succeeded"])

    def test_next_admission_prunes_a_disconnected_stalled_request(self):
        disconnected = threading.Event()
        first = self.stream("stall", is_disconnected=disconnected.is_set)
        received = []
        consumer = threading.Thread(target=lambda: received.extend(first.chunks))
        consumer.start()
        try:
            self.assertTrue(self.adapter.started.wait(2))
            disconnected.set()
            self.assertEqual(list(self.stream().chunks), [b"data: [DONE]\n\n"])
            self.assertTrue(self.adapter.stopped.wait(1))
            consumer.join(2)
            self.assertFalse(consumer.is_alive())
            self.assertEqual(received, [])
            self.assertEqual([event["outcome"] for event in self.audit], ["cancelled", "succeeded"])
        finally:
            self.adapter.stopped.set()
            if hasattr(first.chunks, "close"):
                first.chunks.close()
            consumer.join(6)
