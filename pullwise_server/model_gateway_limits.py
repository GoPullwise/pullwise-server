from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


class GatewayLimitExceeded(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GatewayLimitPolicy:
    worker_requests_per_minute: int
    route_requests_per_minute: int
    worker_concurrency: int
    route_concurrency: int
    worker_output_tokens_per_minute: int
    route_output_tokens_per_minute: int

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("worker_requests_per_minute", self.worker_requests_per_minute, 100_000),
            ("route_requests_per_minute", self.route_requests_per_minute, 1_000_000),
            ("worker_concurrency", self.worker_concurrency, 1_024),
            ("route_concurrency", self.route_concurrency, 4_096),
            ("worker_output_tokens_per_minute", self.worker_output_tokens_per_minute, 1_000_000_000),
            ("route_output_tokens_per_minute", self.route_output_tokens_per_minute, 1_000_000_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} is invalid")


class GatewayLimiter:
    def __init__(self, *, policy: GatewayLimitPolicy, clock: Callable[[], float]) -> None:
        self._policy = policy
        self._clock = clock
        self._lock = threading.Lock()
        self._window = -1
        self._worker_requests: dict[str, int] = {}
        self._route_requests: dict[str, int] = {}
        self._worker_output_tokens: dict[str, int] = {}
        self._route_output_tokens: dict[str, int] = {}
        self._worker_active: dict[str, int] = {}
        self._route_active: dict[str, int] = {}

    @contextmanager
    def acquire(self, *, worker_id: str, route_id: str, output_tokens: int) -> Iterator[Callable[[int], None]]:
        with self._lock:
            window = int(self._clock() // 60)
            if window != self._window:
                self._window = window
                self._worker_requests.clear()
                self._route_requests.clear()
                self._worker_output_tokens.clear()
                self._route_output_tokens.clear()
            worker_requests = self._worker_requests.get(worker_id, 0)
            route_requests = self._route_requests.get(route_id, 0)
            if (
                worker_requests >= self._policy.worker_requests_per_minute
                or route_requests >= self._policy.route_requests_per_minute
            ):
                raise GatewayLimitExceeded("GATEWAY_RATE_LIMITED")
            worker_tokens = self._worker_output_tokens.get(worker_id, 0)
            route_tokens = self._route_output_tokens.get(route_id, 0)
            if (
                worker_tokens + output_tokens > self._policy.worker_output_tokens_per_minute
                or route_tokens + output_tokens > self._policy.route_output_tokens_per_minute
            ):
                raise GatewayLimitExceeded("GATEWAY_BUDGET_EXCEEDED")
            worker_active = self._worker_active.get(worker_id, 0)
            route_active = self._route_active.get(route_id, 0)
            if (
                worker_active >= self._policy.worker_concurrency
                or route_active >= self._policy.route_concurrency
            ):
                raise GatewayLimitExceeded("GATEWAY_CONCURRENCY_LIMITED")
            self._worker_requests[worker_id] = worker_requests + 1
            self._route_requests[route_id] = route_requests + 1
            self._worker_output_tokens[worker_id] = worker_tokens + output_tokens
            self._route_output_tokens[route_id] = route_tokens + output_tokens
            self._worker_active[worker_id] = worker_active + 1
            self._route_active[route_id] = route_active + 1
        settled = False

        def settle(actual_output_tokens: int) -> None:
            nonlocal settled
            if type(actual_output_tokens) is not int or not 0 <= actual_output_tokens <= 1_000_000_000:
                raise ValueError("actual output token usage is invalid")
            with self._lock:
                if settled:
                    return
                settled = True
                if window != self._window:
                    return
                adjustment = actual_output_tokens - output_tokens
                self._worker_output_tokens[worker_id] += adjustment
                self._route_output_tokens[route_id] += adjustment
        try:
            yield settle
        finally:
            with self._lock:
                self._decrement(self._worker_active, worker_id)
                self._decrement(self._route_active, route_id)

    @staticmethod
    def _decrement(counts: dict[str, int], key: str) -> None:
        remaining = counts.get(key, 0) - 1
        if remaining > 0:
            counts[key] = remaining
        else:
            counts.pop(key, None)
