"""Local process boundary for one bounded raw Jev response.

The callback runs in a spawned child. The parent owns the monotonic deadline,
response size and process cleanup. No SDK client or credential is created here.
"""
from __future__ import annotations

import math
import multiprocessing
import time
from collections.abc import Callable, Mapping
from multiprocessing.sharedctypes import RawArray

from .typesafe_client import DEFAULT_JEV_MODEL, MAX_RESPONSE_BYTES, build_request


def _child(send, shared, invoke: Callable, request: dict, max_bytes: int) -> None:
    try:
        raw = invoke(state=request["state"], questions=request["questions"],
            model=DEFAULT_JEV_MODEL)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= max_bytes:
            send.send((False, "invalid"))
            return
        shared[:len(raw)] = raw
        send.send((True, len(raw)))
    except BaseException:
        # Provider exception text can contain credentials or response content.
        send.send((False, "unavailable"))
    finally:
        send.close()


class BoundedJevRawTransport:
    """Return bounded raw bytes or terminate the provider child at the deadline.

    `invoke` must be a spawn-safe callable that creates and closes its own SDK
    client in the child. It receives only a validated text request. This class
    is local Python infrastructure and is not wired into default collection.
    """

    def __init__(self, *, invoke: Callable, total_seconds: float = 30,
                 max_bytes: int = MAX_RESPONSE_BYTES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(invoke) or not callable(clock):
            raise ValueError("JEV_TRANSPORT_CALLABLE_INVALID")
        if (isinstance(total_seconds, bool) or not isinstance(total_seconds, (int, float))
                or not math.isfinite(total_seconds) or not 0 < total_seconds <= 90):
            raise ValueError("JEV_TOTAL_DEADLINE_INVALID")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("JEV_RESPONSE_LIMIT_INVALID")
        self.invoke = invoke
        self.total_seconds = float(total_seconds)
        self.max_bytes = max_bytes
        self.clock = clock

    def __call__(self, *, state: object, questions: Mapping[str, Mapping[str, object]],
                 model: str) -> bytes:
        request = build_request(state=state, questions=questions, model=model)
        deadline = self.clock() + self.total_seconds
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        shared = RawArray("B", self.max_bytes)
        process = context.Process(target=_child,
            args=(send, shared, self.invoke, request, self.max_bytes), daemon=True)
        started = False
        try:
            process.start()
            started = True
            send.close()
            process.join(timeout=max(0, deadline - self.clock()))
            if process.is_alive() or self.clock() >= deadline:
                raise TimeoutError("JEV_TOTAL_DEADLINE")
            if not receive.poll(0):
                raise OSError("JEV_PROVIDER_UNAVAILABLE")
            success, result = receive.recv()
            if not success:
                if result == "invalid":
                    raise ValueError("JEV_RESPONSE_INVALID")
                raise OSError("JEV_PROVIDER_UNAVAILABLE")
            if type(result) is not int or not 0 < result <= self.max_bytes:
                raise ValueError("JEV_RESPONSE_INVALID")
            return bytes(shared[:result])
        finally:
            receive.close()
            send.close()
            if started:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=0.25)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=0.25)
