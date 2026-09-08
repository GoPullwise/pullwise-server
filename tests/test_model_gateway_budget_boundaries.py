import itertools
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pullwise_server.model_gateway_adapters import OpenAICompletionsAdapter
from pullwise_server.model_gateway_limits import GatewayLimiter, GatewayLimitPolicy
from pullwise_server.model_gateway_runtime import GatewayRequestError, GatewayRoute, ModelGatewayRuntime, StaticGatewayRouteResolver

NOW = 1_788_259_200
ROUTE = GatewayRoute(route_id="primary", profile_set_id="profile", profile_revision=1,
    manifest_digest="a" * 64, model_alias="reviewer", provider_connection_id="provider",
    upstream_provider="openai", adapter="openai-completions", endpoint_origin="https://api.openai.com",
    upstream_model="gpt-5.5", secret_ref="provider/test", secret_version="v1")


def make_runtime(policy, routes=(ROUTE,), adapter=None):
    limiter = GatewayLimiter(policy=policy, clock=lambda: NOW)
    store = Mock(read_version=Mock(return_value=b"synthetic"))
    identifiers = itertools.count()
    runtime = ModelGatewayRuntime(token_verifier=SimpleNamespace(verify=lambda *args, **kwargs: None),
        route_resolver=StaticGatewayRouteResolver(routes), secret_store=store,
        adapters={"openai-completions": adapter or OpenAICompletionsAdapter()},
        audit_sink=lambda event: None, request_id_factory=lambda: f"request-{next(identifiers)}",
        clock=lambda: NOW, limiter=limiter)
    return runtime, limiter, store


def request(runtime, *, worker="worker", profile="profile", revision=1, **payload):
    return runtime.chat_completions(worker_id=worker, profile_set_id=profile,
        profile_revision=revision, bearer_token="synthetic", payload={"model": "reviewer",
            "messages": [{"role": "user", "content": "fixture"}], **payload})


@pytest.mark.parametrize("stream", [False, True])
def test_multiple_choices_reserve_aggregate_output_before_secret_or_upstream(stream):
    runtime, limiter, store = make_runtime(GatewayLimitPolicy(60, 60, 1, 1, 150, 150))
    with patch("pullwise_server.model_gateway_adapters.open_upstream") as upstream:
        with pytest.raises(GatewayRequestError, match="GATEWAY_BUDGET_EXCEEDED"):
            request(runtime, max_tokens=100, n=2, stream=stream)
        upstream.assert_not_called()
        store.read_version.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_accepted_multiple_choices_forward_per_choice_ceiling_and_settle_total_once(stream):
    runtime, limiter, store = make_runtime(GatewayLimitPolicy(60, 60, 1, 1, 200, 200))
    def upstream(_url, *, payload, **_kwargs):
        assert payload["max_tokens"] == 100
        assert payload["n"] == 2
        assert limiter._worker_output_tokens["worker"] == 200
        response = {"choices": [], "usage": {"completion_tokens": 120}}
        wire = (f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n" if stream else json.dumps(response)).encode()
        return SimpleNamespace(status=200, headers={"Content-Type": "text/event-stream" if stream else "application/json"},
            iter_bytes=lambda _size: iter([wire]), close=lambda: None)
    with patch("pullwise_server.model_gateway_adapters.open_upstream", upstream):
        response = request(runtime, max_tokens=100, n=2, stream=stream)
        if stream:
            list(response.chunks)
    assert store.read_version.call_count == 1
    assert limiter._worker_output_tokens["worker"] == 120


@pytest.mark.parametrize("choices", [0, -1, 129, True, None, "2", 1.5])
def test_choice_count_is_a_bounded_integer(choices):
    runtime, _, store = make_runtime(GatewayLimitPolicy(60, 60, 1, 1, 1000000, 1000000))
    with pytest.raises(GatewayRequestError, match="GATEWAY_CHOICE_COUNT_INVALID"):
        request(runtime, max_tokens=1, n=choices)
    store.read_version.assert_not_called()


@pytest.mark.parametrize("ceiling", [{}, {"max_tokens": 3}, {"max_completion_tokens": 3}])
def test_default_and_explicit_single_choice_ceilings_are_forwarded(ceiling):
    calls = []
    adapter = SimpleNamespace(complete=lambda _route, _secret, payload, **_kwargs:
        calls.append(payload) or {"choices": []})
    runtime, limiter, _ = make_runtime(GatewayLimitPolicy(60, 60, 1, 1, 100000, 100000), adapter=adapter)
    request(runtime, n=1, **ceiling)
    assert calls[0].get("max_tokens", calls[0].get("max_completion_tokens")) == (3 if ceiling else 16384)
    assert limiter._worker_output_tokens["worker"] == (3 if ceiling else 16384)


@pytest.mark.parametrize("policy,code", [
    (GatewayLimitPolicy(60, 1, 2, 2, 100, 100), "GATEWAY_RATE_LIMITED"),
    (GatewayLimitPolicy(60, 60, 2, 2, 10, 1), "GATEWAY_BUDGET_EXCEEDED"),
])
def test_route_limits_are_isolated_by_profile_and_shared_across_revisions(policy, code):
    routes = (ROUTE, replace(ROUTE, profile_set_id="other", provider_connection_id="other-provider"),
              replace(ROUTE, profile_revision=2))
    adapter = SimpleNamespace(complete=lambda *args, **kwargs: {"choices": [], "usage": {"completion_tokens": 1}})
    runtime, _, _ = make_runtime(policy, routes, adapter)
    request(runtime, max_tokens=1, worker="worker-a")
    request(runtime, max_tokens=1, worker="worker-b", profile="other")
    with pytest.raises(GatewayRequestError, match=code):
        request(runtime, max_tokens=1, worker="worker-c", revision=2)


def test_concurrent_streams_share_capacity_only_within_the_same_profile_route():
    routes = (ROUTE, replace(ROUTE, profile_set_id="other"), replace(ROUTE, profile_revision=2))
    runtime, _, _ = make_runtime(GatewayLimitPolicy(60, 60, 2, 1, 100, 100), routes)
    first = request(runtime, max_tokens=1, worker="worker-a", stream=True)
    second = None
    try:
        second = request(runtime, max_tokens=1, worker="worker-b", profile="other", stream=True)
        with pytest.raises(GatewayRequestError, match="GATEWAY_CONCURRENCY_LIMITED"):
            request(runtime, max_tokens=1, worker="worker-c", revision=2, stream=True)
    finally:
        first.chunks.close()
        if second:
            second.chunks.close()
