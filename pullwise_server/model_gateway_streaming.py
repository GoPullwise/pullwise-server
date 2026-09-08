"""Bounded response pumping and cancellation independent of upstream read cadence."""
from __future__ import annotations

import queue
import threading
from typing import Callable, Generic, Iterable, Iterator, TypeVar

T = TypeVar("T")


class RequestCancellation:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def add_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        callback()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass


class ManagedResponse(Iterator[T], Generic[T]):
    def __init__(self, *, producer: Callable[[RequestCancellation], Iterable[T]],
                 finish: Callable[[str], None], is_disconnected: Callable[[], bool] | None = None) -> None:
        self.cancellation = RequestCancellation()
        self._producer = producer
        self._finish_callback = finish
        self._is_disconnected = is_disconnected
        self._queue: queue.Queue[T] = queue.Queue(maxsize=16)
        self._lock = threading.RLock()
        self._started = False
        self._outcome: str | None = None
        self._error: BaseException | None = None

    def __iter__(self) -> "ManagedResponse[T]":
        return self

    def _finish(self, outcome: str) -> None:
        with self._lock:
            if self._outcome is not None:
                return
            self._outcome = outcome
            self.cancellation.cancel()
            self._finish_callback(outcome)

    def close(self) -> None:
        self._finish("cancelled")

    def cancel_if_disconnected(self) -> None:
        if self._is_disconnected is not None and self._is_disconnected():
            self.close()

    def _pump(self) -> None:
        iterator = None
        try:
            iterator = iter(self._producer(self.cancellation))
            for item in iterator:
                while not self.cancellation.is_set():
                    try:
                        self._queue.put(item, timeout=0.05)
                        break
                    except queue.Full:
                        continue
                if self.cancellation.is_set():
                    return
            self._finish("succeeded")
        except BaseException as error:
            with self._lock:
                if self._outcome is None:
                    self._error = error
                    self._finish("failed")
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    def __next__(self) -> T:
        self.cancel_if_disconnected()
        with self._lock:
            if not self._started and self._outcome is None:
                self._started = True
                threading.Thread(target=self._pump, name="gateway-upstream", daemon=True).start()
        while True:
            self.cancel_if_disconnected()
            with self._lock:
                if self._outcome == "cancelled":
                    raise StopIteration
                if self._outcome is not None and self._queue.empty():
                    if self._error:
                        raise self._error
                    raise StopIteration
            try:
                item = self._queue.get(timeout=0.05)
                self.cancel_if_disconnected()
                with self._lock:
                    if self._outcome == "cancelled":
                        raise StopIteration
                return item
            except queue.Empty:
                continue


class ActiveResponses:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, set[ManagedResponse]] = {}
        self._routes: dict[str, set[ManagedResponse]] = {}

    def add(self, worker: str, route: str, response: ManagedResponse) -> None:
        with self._lock:
            self._workers.setdefault(worker, set()).add(response)
            self._routes.setdefault(route, set()).add(response)

    def remove(self, worker: str, route: str, response: ManagedResponse) -> None:
        with self._lock:
            for index, key in ((self._workers, worker), (self._routes, route)):
                members = index.get(key)
                if members is not None:
                    members.discard(response)
                    if not members:
                        index.pop(key, None)

    def prune_disconnected(self, worker: str, route: str) -> None:
        with self._lock:
            responses = self._workers.get(worker, set()) | self._routes.get(route, set())
        for response in responses:
            response.cancel_if_disconnected()
