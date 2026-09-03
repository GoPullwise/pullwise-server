from __future__ import annotations

import json
import unittest

from pullwise_server.model_gateway_http import HttpStreamingResult, ModelGatewayHttpApplication
from pullwise_server.model_gateway_runtime import GatewayStreamingResponse


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def chat_completions(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        payload = kwargs.get("payload")
        if isinstance(payload, dict) and payload.get("stream") is True:
            return GatewayStreamingResponse(
                chunks=iter([b'data: {"id":"chunk-1"}\n\n', b"data: [DONE]\n\n"]),
            )
        return {"id": "chatcmpl_http", "choices": []}


class FakeBroker:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def put_candidate(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"version": "version-1", "fingerprint": "sha256:0123456789ab"}


class ModelGatewayHttpApplicationTest(unittest.TestCase):
    def test_only_closed_broker_and_scoped_completion_routes_are_exposed(self) -> None:
        runtime = FakeRuntime()
        broker = FakeBroker()
        application = ModelGatewayHttpApplication(runtime=runtime, secret_broker=broker)

        stored = application.dispatch(
            method="POST",
            path="/internal/provider-secrets",
            headers={"Authorization": "Bearer broker-write-token"},
            body=json.dumps(
                {"secret_ref": "provider/openai/secret_1", "secret": "upstream-secret"}
            ).encode("utf-8"),
        )
        completed = application.dispatch(
            method="POST",
            path=(
                "/v1/workers/worker_a/profiles/reviewer-production/"
                "revisions/12/chat/completions"
            ),
            headers={"Authorization": "Bearer gateway-access-token"},
            body=json.dumps(
                {"model": "gpt-reviewer", "messages": [{"role": "user", "content": "prompt"}]}
            ).encode("utf-8"),
        )
        arbitrary = application.dispatch(
            method="POST",
            path="/proxy/https://attacker.example/steal",
            headers={"Authorization": "Bearer gateway-access-token"},
            body=b"{}",
        )
        streamed = application.dispatch(
            method="POST",
            path=(
                "/v1/workers/worker_a/profiles/reviewer-production/"
                "revisions/12/chat/completions"
            ),
            headers={"Authorization": "Bearer gateway-access-token"},
            body=json.dumps(
                {
                    "model": "gpt-reviewer",
                    "messages": [{"role": "user", "content": "prompt"}],
                    "stream": True,
                }
            ).encode("utf-8"),
        )

        self.assertEqual(stored.status, 201)
        self.assertEqual(stored.payload, {"version": "version-1", "fingerprint": "sha256:0123456789ab"})
        self.assertEqual(completed.status, 200)
        self.assertEqual(completed.payload["id"], "chatcmpl_http")
        self.assertEqual(
            runtime.calls[0],
            {
                "worker_id": "worker_a",
                "profile_set_id": "reviewer-production",
                "profile_revision": 12,
                "bearer_token": "gateway-access-token",
                "payload": {
                    "model": "gpt-reviewer",
                    "messages": [{"role": "user", "content": "prompt"}],
                },
            },
        )
        self.assertEqual(arbitrary.status, 404)
        self.assertIsInstance(streamed, HttpStreamingResult)
        self.assertEqual(list(streamed.chunks), [b'data: {"id":"chunk-1"}\n\n', b"data: [DONE]\n\n"])
        self.assertEqual(len(runtime.calls), 2)
        self.assertNotIn("upstream-secret", json.dumps(stored.payload))

        oversized = application.dispatch(
            method="POST",
            path=(
                "/v1/workers/worker_a/profiles/reviewer-production/"
                "revisions/12/chat/completions"
            ),
            headers={"Authorization": "Bearer gateway-access-token"},
            body=b"x" * (1024 * 1024 + 1),
        )
        self.assertEqual(oversized.status, 400)
        self.assertEqual(oversized.payload["error"]["code"], "GATEWAY_REQUEST_BODY_INVALID")


if __name__ == "__main__":
    unittest.main()
