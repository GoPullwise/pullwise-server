from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from pullwise_server.model_gateway_adapters import (
    OpenAICompletionsAdapter,
    OpenAIConnectionValidator,
)
from pullwise_server.model_gateway_runtime import GatewayRoute


class FakeStreamingResponse:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self) -> None:
        self.closed = False

    def iter_content(self, chunk_size: int):
        self.chunk_size = chunk_size
        yield b'data: {"id":"chunk-1"}\n\n'
        yield b"data: [DONE]\n\n"

    def close(self) -> None:
        self.closed = True


class FakeModelResponse(FakeStreamingResponse):
    headers = {"Content-Type": "application/json"}

    def iter_content(self, chunk_size: int):
        self.chunk_size = chunk_size
        yield b'{"object":"list","data":[{"id":"gpt-5.5","object":"model","owned_by":"openai"}]}'


class ModelGatewayAdapterTest(unittest.TestCase):
    def test_gateway_preserves_the_model_sdk_payload_without_rewriting_messages(self) -> None:
        route = GatewayRoute(
            route_id="flash", profile_set_id="live", profile_revision=1,
            manifest_digest="a" * 64, model_alias="flash",
            provider_connection_id="deepseek", upstream_provider="deepseek",
            adapter="openai-completions", endpoint_origin="https://api.deepseek.com",
            upstream_model="deepseek-v4-flash", secret_ref="provider/deepseek/key", secret_version="v1",
        )
        request = {
            "model": "deepseek-v4-flash", "stream": True,
            "max_tokens": 16384, "reasoning_effort": "low", "thinking": {"type": "enabled"},
            "messages": [
                {"role": "system", "content": "Review read-only."},
                {"role": "user", "content": "Review this repository."},
                {"role": "assistant", "content": "", "reasoning_content": "provider-owned-context",
                 "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "repo_ls", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call1", "content": "a.ts"},
            ],
        }
        original = deepcopy(request)
        with patch("pullwise_server.model_gateway_adapters.requests.post", return_value=FakeStreamingResponse()) as post:
            list(OpenAICompletionsAdapter().stream(route, b"test-key", request))
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent, original)
        self.assertEqual(request, original)

    def test_connection_validation_uses_bounded_fixed_models_probe(self) -> None:
        response = FakeModelResponse()
        with patch(
            "pullwise_server.model_gateway_adapters.requests.get",
            return_value=response,
        ) as get:
            validator = OpenAIConnectionValidator(timeout_seconds=10)
            models = validator.validate(
                endpoint_origin="https://api.openai.com",
                adapter="openai-completions",
                provider="openai",
                secret=b"upstream-secret",
            )
            validator.canary(
                endpoint_origin="https://api.openai.com",
                adapter="openai-completions",
                provider="openai",
                secret=b"upstream-secret",
            )
        self.assertEqual(models, ["gpt-5.5"])
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            args, kwargs = call
            self.assertEqual(args, ("https://api.openai.com/v1/models",))
            self.assertEqual(kwargs["headers"], {"Authorization": "Bearer upstream-secret"})
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(kwargs["stream"])

    def test_stream_uses_fixed_upstream_url_headers_and_closes_response(self) -> None:
        route = GatewayRoute(
            route_id="gpt-primary",
            profile_set_id="reviewer-production",
            profile_revision=12,
            manifest_digest="a" * 64,
            model_alias="gpt-reviewer",
            provider_connection_id="openai-production",
            upstream_provider="openai",
            adapter="openai-completions",
            endpoint_origin="https://api.openai.com",
            upstream_model="gpt-5.5",
            secret_ref="provider/openai-production/secret_1",
            secret_version="version-1",
        )
        response = FakeStreamingResponse()
        with patch(
            "pullwise_server.model_gateway_adapters.requests.post",
            return_value=response,
        ) as post:
            chunks = list(
                OpenAICompletionsAdapter(timeout_seconds=30).stream(
                    route,
                    b"upstream-secret",
                    {
                        "model": "gpt-5.5",
                        "messages": [{"role": "user", "content": "prompt"}],
                        "stream": True,
                    },
                )
            )

        self.assertEqual(chunks, [b'data: {"id":"chunk-1"}\n\n', b"data: [DONE]\n\n"])
        self.assertTrue(response.closed)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args, ("https://api.openai.com/v1/chat/completions",))
        self.assertEqual(
            kwargs["headers"],
            {
                "Authorization": "Bearer upstream-secret",
                "Content-Type": "application/json",
            },
        )
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])

        deepseek_response = FakeStreamingResponse()
        with patch(
            "pullwise_server.model_gateway_adapters.requests.post",
            return_value=deepseek_response,
        ) as deepseek_post:
            list(
                OpenAICompletionsAdapter(timeout_seconds=30).stream(
                    replace(
                        route,
                        provider_connection_id="deepseek-production",
                        upstream_provider="deepseek",
                        endpoint_origin="https://api.deepseek.com",
                        upstream_model="deepseek-v4-pro",
                    ),
                    b"deepseek-secret",
                    {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "prompt"}], "stream": True},
                )
            )
        self.assertEqual(deepseek_post.call_args.args, ("https://api.deepseek.com/chat/completions",))


if __name__ == "__main__":
    unittest.main()
