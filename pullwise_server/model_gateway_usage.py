from __future__ import annotations

import json
import re


def reported_output_tokens(payload: object) -> int | None:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    value = usage.get("completion_tokens") if isinstance(usage, dict) else None
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


class CompletionStreamUsage:
    """Read standard completion usage without retaining or changing SSE content."""

    MAX_FRAME_BYTES = 1024 * 1024

    def __init__(self) -> None:
        self.output_tokens: int | None = None
        self._buffer = b""
        self._discard = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while True:
            delimiter = re.search(rb"\r?\n\r?\n", self._buffer)
            if delimiter is None:
                if len(self._buffer) > self.MAX_FRAME_BYTES:
                    self._buffer = self._buffer[-3:]
                    self._discard = True
                return
            frame, self._buffer = self._buffer[:delimiter.start()], self._buffer[delimiter.end():]
            if self._discard or len(frame) > self.MAX_FRAME_BYTES:
                self._discard = False
                continue
            data = b"\n".join(line[5:].lstrip(b" ") for line in frame.splitlines() if line.startswith(b"data:"))
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError):
                continue
            tokens = reported_output_tokens(payload)
            if tokens is not None:
                self.output_tokens = tokens
